"""
Container Service - Business logic for container lifecycle
"""
import logging
import re as _re_top
import unicodedata
from datetime import datetime, timedelta
from flask import request
from CTFd.models import db
from CTFd.utils import get_config
from ..models.instance import ContainerInstance
from ..models.challenge import ContainerChallenge
from ..models.audit import ContainerAuditLog
from .docker_service import DockerService
from .flag_service import FlagService
from .port_manager import PortManager

logger = logging.getLogger(__name__)



def _slugify(text: str) -> str:
    """Normalize unicode, strip non-ASCII accents, lowercase, replace spaces/separators with hyphens."""
    # Decompose unicode (e.g. 'Đ' → 'D' + combining stroke, 'á' → 'a' + combining acute)
    nfkd = unicodedata.normalize('NFKD', text)
    ascii_text = nfkd.encode('ascii', 'ignore').decode('ascii')
    # Lowercase, replace whitespace and underscores with hyphens, strip non-alphanumeric
    slug = _re_top.sub(r'[^a-z0-9]+', '-', ascii_text.lower()).strip('-')
    return slug or 'user'


def _extract_instance_prefix(account_id: int) -> str:
    """
    Build the per-instance subdomain prefix from the account name.

    Strategy:
      1. Look for a student/employee ID: a run of 6+ digits anywhere in the name,
         commonly after ' - ' (e.g. 'Đoàn Văn Sáng - 20301111').
      2. Fall back to slugifying the full account name.
      3. Last resort: 'user-<account_id>'.
    """
    try:
        mode = get_config('user_mode')
        if mode == 'teams':
            from CTFd.models import Teams
            account = Teams.query.get(account_id)
        else:
            from CTFd.models import Users
            account = Users.query.get(account_id)

        if not account:
            return f'user-{account_id}'

        name = account.name or ''

        # 1. Extract student ID: 6+ consecutive digits, preferring the segment after ' - '
        #    e.g. "Đoàn Văn Sáng - 20301111"  →  "20301111"
        #         "Nguyen Van A (2030xxx)"      →  "2030xxx"
        parts = _re_top.split(r'\s*[-–—]\s*', name)
        for part in reversed(parts):          # check right-side parts first
            m = _re_top.search(r'\b(\d{6,})\b', part)
            if m:
                return m.group(1)

        # 2. Slugify the full name
        slug = _slugify(name)
        # Cap at 32 chars to keep subdomains reasonable
        return slug[:32].strip('-') or f'user-{account_id}'

    except Exception:
        return f'user-{account_id}'


class ContainerService:
    """
    Service to manage container lifecycle
    """
    
    def __init__(self, docker_service: DockerService, flag_service: FlagService, port_manager: PortManager, notification_service=None):
        self.docker = docker_service
        self.flag_service = flag_service
        self.port_manager = port_manager
        self.notification_service = notification_service
        self._cleanup_running = False  # Prevent overlapping cleanup jobs
    
    def create_instance(self, challenge_id: int, account_id: int, user_id: int) -> ContainerInstance:
        """
        Create new container instance
        
        Args:
            challenge_id: Challenge ID
            account_id: Team ID (team mode) or User ID (user mode)
            user_id: Actual user ID creating container
        
        Returns:
            ContainerInstance object
        
        Raises:
            Exception if error occurs
        """
        # 1. Validate challenge exists
        challenge = ContainerChallenge.query.get(challenge_id)
        if not challenge:
            raise Exception("Challenge not found")
        
        # 2. Check if already solved (prevent creating instance after solve)
        # removed check
        
        # 3. Check if already has running instance
        existing = ContainerInstance.query.filter_by(
            challenge_id=challenge_id,
            account_id=account_id,
            status='running'
        ).first()
        
        if existing and not existing.is_expired():
            logger.info(f"Account {account_id} already has running instance for challenge {challenge_id}")
            return existing
        
        # 4. Stop any existing expired instances
        if existing and existing.is_expired():
            logger.info(f"Stopping expired instance {existing.uuid}")
            self.stop_instance(existing, user_id, reason='expired')
        
        # 5. Create instance record (status=pending)
        # Set expiration based on global timeout setting
        expires_at = datetime.utcnow() + timedelta(minutes=challenge.get_timeout_minutes())
        
        # Generate flag
        flag_plaintext = self.flag_service.generate_flag(challenge, account_id=account_id)
        flag_encrypted = self.flag_service.encrypt_flag(flag_plaintext)
        flag_hash = self.flag_service.hash_flag(flag_plaintext)
        
        instance = ContainerInstance(
            challenge_id=challenge_id,
            account_id=account_id,
            flag_encrypted=flag_encrypted,
            flag_hash=flag_hash,
            status='pending',
            expires_at=expires_at
        )
        
        db.session.add(instance)
        db.session.flush()  # Get instance ID
        
        # 6. Create flag record (only for random flag mode - anti-cheat tracking)
        if challenge.flag_mode == 'random':
            self.flag_service.create_flag_record(instance, challenge, account_id, flag_plaintext)
        
        # 7. Audit log
        self._create_audit_log(
            'instance_created',
            instance_id=instance.id,
            challenge_id=challenge_id,
            account_id=account_id,
            user_id=user_id,
            details={'expires_at': expires_at.isoformat()}
        )
        
        db.session.commit()
        
        # 8. Provision container (async)
        try:
            self._provision_container(instance, challenge, flag_plaintext)
        except Exception as e:
            logger.error(f"Failed to provision container: {e}")
            instance.status = 'error'
            instance.extra_data = {'error': str(e)}
            db.session.commit()
            raise
        
        return instance
    
    def _provision_container(self, instance: ContainerInstance, challenge: ContainerChallenge, flag: str):
        """
        Provision Docker container(s)
        
        Supports both single-container and multi-container (compose) mode.
        Multi-container mode is triggered when challenge.compose_config is set.
        """
        import uuid as uuid_module
        
        # Update status
        instance.status = 'provisioning'
        db.session.commit()
        
        try:
            # Check for multi-container (compose) mode
            if challenge.compose_config and challenge.compose_config.strip():
                self._provision_compose(instance, challenge, flag)
                return
            # Get config
            from ..models.config import ContainerConfig
            
            # Check if subdomain routing is enabled
            subdomain_enabled = ContainerConfig.get('subdomain_enabled', 'false').lower() == 'true'
            subdomain_base_domain = ContainerConfig.get('subdomain_base_domain', '')
            subdomain_network = ContainerConfig.get('subdomain_network', 'ctfd-network')
            
            # Determine if this challenge should use subdomain routing
            # Only for HTTP/web challenges
            use_subdomain = (
                subdomain_enabled and 
                subdomain_base_domain and
                challenge.container_connection_type in ('http', 'https', 'web')
            )
            
            # Retry loop for race conditions (max 5 retries)
            max_retries = 5
            import time
            
            for attempt in range(max_retries):
                try:
                    # 1. Allocate ports
                    host_port = None
                    ports_map = None
                    
                    if challenge.internal_ports:
                        try:
                            int_ports = [int(p.strip()) for p in challenge.internal_ports.split(',') if p.strip()]
                            if int_ports:
                                allocated = self.port_manager.allocate_ports(len(int_ports))
                                ports_map = dict(zip([str(p) for p in int_ports], allocated))
                                # Use the first one as primary for fallback/compatibility
                                host_port = allocated[0]
                        except Exception as e:
                            logger.error(f"Failed to parse/allocate internal_ports: {e}")
                            raise
                    
                    if not ports_map:
                        # Fallback to single port
                        host_port = self.port_manager.allocate_port()
                        ports_map = {str(challenge.internal_port): host_port}

                    

                    
                    # 2. Get connection host
                    connection_host = ContainerConfig.get('connection_host', 'localhost')

                    # 3. Determine Network
                    # TAILNET MODE: All challenges share network with tailscale-challenges
                    # so they are only accessible via the tailnet IP.
                    # Subdomain mode uses its own network for Traefik routing.
                    
                    if use_subdomain:
                        target_network = subdomain_network
                        # Generate random 16-char subdomain with prefix format for Cloudflare Free SSL
                        subdomain = f"c-{uuid_module.uuid4().hex[:16]}"
                        full_hostname = f"{subdomain}.{subdomain_base_domain}"
                        logger.info(f"Generated subdomain: {full_hostname}")
                    else:
                        subdomain = None
                        full_hostname = None
                    
                    # 4. Create Docker container
                    # Generate container name: challengename_accountid
                    import re
                    # Sanitize challenge name (only alphanumeric and hyphens)
                    safe_name = re.sub(r'[^a-zA-Z0-9-]', '', challenge.name.replace(' ', '-').lower())
                    container_name = f"{safe_name}_{instance.account_id}"
                    
                    # Replace {FLAG} placeholder in command if present
                    command = challenge.command if challenge.command else None
                    if command and '{FLAG}' in command:
                        command = command.replace('{FLAG}', flag)
                    
                    # Base labels
                    labels = {
                        'ctfd.instance_uuid': instance.uuid,
                        'ctfd.challenge_id': str(challenge.id),
                        'ctfd.account_id': str(instance.account_id),
                        'ctfd.expires_at': str(instance.expires_at.timestamp())
                    }
                    
                    # Add Traefik labels if subdomain routing is enabled
                    if use_subdomain:
                        labels.update({
                            'traefik.enable': 'true',
                            'traefik.docker.network': subdomain_network,
                        })
                        
                        # Handle multiple ports
                        target_ports = [challenge.internal_port]
                        if challenge.internal_ports:
                            # If explicit multiple ports defined
                            try:
                                pt_list = [int(p.strip()) for p in challenge.internal_ports.split(',') if p.strip()]
                                if pt_list:
                                    target_ports = pt_list
                            except:
                                pass
                                
                        for p in target_ports:
                            # Router name must be unique per port
                            # Format: ctfd-{uuid}-{port}
                            port_suffix = f"-{p}" if str(p) != str(challenge.internal_port) else ""
                            router_name = f"ctfd-{instance.uuid[:8]}{port_suffix}"
                            
                            # Subdomain: 
                            # Main port = random-uuid
                            # Other ports = random-uuid-port
                            current_subdomain = subdomain if str(p) == str(challenge.internal_port) else f"{subdomain}-{p}"
                            current_hostname = f"{current_subdomain}.{subdomain_base_domain}"
                            
                            current_service_name = f"{router_name}-service"

                            labels.update({
                                f'traefik.http.routers.{router_name}.rule': f'Host(`{current_hostname}`)',
                                f'traefik.http.routers.{router_name}.entrypoints': 'web',
                                f'traefik.http.routers.{router_name}.service': current_service_name,
                                f'traefik.http.services.{current_service_name}.loadbalancer.server.port': str(p),
                            })
                    
                    # Create container with tailnet network mode (non-subdomain)
                    # or with target_network (subdomain/traefik)
                    if use_subdomain:
                        result = self.docker.create_container(
                            image=challenge.image,
                            internal_port=challenge.internal_port,
                            host_port=host_port,
                            ports=ports_map,
                            command=command,
                            environment={'FLAG': flag},
                            memory_limit=challenge.get_memory_limit(),
                            cpu_limit=challenge.get_cpu_limit(),
                            pids_limit=challenge.pids_limit,
                            name=container_name,
                            labels=labels,
                            network=target_network,
                            use_traefik=True
                        )
                    else:
                        # PORT BINDING MODE: bind ports directly on host
                        # Using connection_host as bind_ip restricts access to that IP only
                        # (e.g. tailscale IP = only tailnet users can reach the port)
                        result = self.docker.create_container(
                            image=challenge.image,
                            internal_port=challenge.internal_port,
                            host_port=host_port,
                            ports=ports_map,
                            command=command,
                            environment={'FLAG': flag},
                            memory_limit=challenge.get_memory_limit(),
                            cpu_limit=challenge.get_cpu_limit(),
                            pids_limit=challenge.pids_limit,
                            name=container_name,
                            labels=labels,
                            bind_ip=connection_host,
                        )
                    
                    # 5. Update instance
                    instance.container_id = result['container_id']
                    
                    if use_subdomain:
                        instance.connection_port = host_port
                        instance.connection_ports = ports_map
                        # For subdomain routing: store URLs
                        urls = []
                        
                        # Primary port (first one) gets the base subdomain
                        # Others get base-port
                        primary_port = str(challenge.internal_port)
                        
                        # We need to reconstruct the map of internal_port -> subdomain
                        # Actually we already have internal ports from challenge.internal_ports logic above,
                        # but let's be robust.
                        
                        # If we have multiple ports, we generated multiple rules above.
                        # We need to store them in connection_info so frontend can display them.
                        
                        # Re-calculate ports list for consistent ordering
                        target_ports = [challenge.internal_port]
                        if challenge.internal_ports:
                             pt_list = [int(p.strip()) for p in challenge.internal_ports.split(',') if p.strip()]
                             if pt_list:
                                target_ports = pt_list

                        for p in target_ports:
                            p_str = str(p)
                            # Logic must match label generation
                            if p_str == str(challenge.internal_port):
                                s_name = subdomain
                            else:
                                s_name = f"{subdomain}-{p}"
                            
                            f_hostname = f"{s_name}.{subdomain_base_domain}"
                            urls.append({
                                'port': p,
                                'url': f"https://{f_hostname}"
                            })

                        instance.connection_host = full_hostname # Keep primary for backward compat
                        instance.connection_info = {
                            'type': 'url_list',
                            'urls': urls,
                            'subdomain': subdomain,
                            'info': challenge.container_connection_info
                        }
                    else:
                        # TAILNET MODE: use allocated port (challenge reads PORT env to listen)
                        instance.connection_port = host_port
                        instance.connection_ports = ports_map
                        
                        # For port-based routing: store host:port
                        instance.connection_host = connection_host
                        instance.connection_info = {
                            'type': challenge.container_connection_type,
                            'info': challenge.container_connection_info
                        }
                    
                    instance.status = 'running'
                    instance.started_at = datetime.utcnow()
                    
                    db.session.commit()
                    
                    # 6. Schedule expiration in Redis (for accurate killing)
                    try:
                        from .. import redis_expiration_service
                        if redis_expiration_service:
                            expires_in_seconds = int((instance.expires_at - datetime.utcnow()).total_seconds())
                            redis_expiration_service.schedule_expiration(
                                instance.uuid,
                                expires_in_seconds
                            )
                    except Exception as e:
                        logger.warning(f"Failed to schedule Redis expiration: {e}")
                    
                    logger.info(f"Provisioned container {result['container_id'][:12]} for instance {instance.uuid}")
                    if use_subdomain:
                        logger.info(f"Subdomain routing: https://{subdomain}.{subdomain_base_domain}")
                    
                    # Audit log
                    self._create_audit_log(
                        'instance_started',
                        instance_id=instance.id,
                        challenge_id=challenge.id,
                        account_id=instance.account_id,
                        details={
                            'container_id': result['container_id'],
                            'port': host_port,
                            'ports': ports_map,
                            'subdomain': subdomain if use_subdomain else None
                        }
                    )
                    
                    # Success: break loop
                    break
                    
                except Exception as e:
                    logger.warning(f"Attempt {attempt+1}/{max_retries} failed: {e}")
                    # If this was the last attempt, re-raise the exception
                    if attempt == max_retries - 1:
                        logger.error(f"Error provisioning container after {max_retries} attempts: {e}")
                        instance.status = 'error'
                        instance.extra_data = {'error': str(e)}
                        db.session.commit()
                        raise
                    
                    # Wait before retrying (exponential backoff not really needed here, just jitter)
                    import random
                    time.sleep(0.1 + random.random() * 0.2)

            
        except Exception as e:
            logger.error(f"Error provisioning container: {e}")
            
            # Send notification
            if self.notification_service:
                 self.notification_service.notify_error("Container Provisioning", str(e))

            instance.status = 'error'
            instance.extra_data = {'error': str(e)}
            db.session.commit()
            raise
    
    def _provision_compose(self, instance: ContainerInstance, challenge: ContainerChallenge, flag: str):
        """
        Provision multi-container group from compose_config YAML.

        Uses Traefik for routing: containers are tagged with Traefik labels,
        and the Traefik container is connected to the per-instance bridge network.

        Traefik mode is activated when the YAML has `traefik: true` at the top level, OR when
        any container's labels contain `traefik.enable: "true"`.
        """
        import yaml
        import re

        try:
            config = yaml.safe_load(challenge.compose_config)
            containers_list = config.get('containers', [])

            if not containers_list:
                raise Exception("compose_config has no 'containers' defined")

            # --- Traefik-mode detection ---
            use_traefik = bool(config.get('traefik', False))
            if not use_traefik:
                for c in containers_list:
                    if str(c.get('labels', {}).get('traefik.enable', '')).lower() == 'true':
                        use_traefik = True
                        break

            # --- Placeholder substitution for label values ---
            _account_prefix = _extract_instance_prefix(instance.account_id)
            _challenge_slug = _re_top.sub(r'[^a-z0-9]+', '-',
                                          challenge.name.lower()).strip('-')[:24]
            _instance_name = f"{_account_prefix}-{_challenge_slug}-{instance.uuid[:8]}"

            substitutions = {
                '{uuid}': instance.uuid[:8],
                '{full_uuid}': instance.uuid,
                '{account_id}': str(instance.account_id),
                '{challenge_id}': str(challenge.id),
                '{instance_name}': _instance_name,
            }

            def _apply_substitutions(text: str) -> str:
                for placeholder, value in substitutions.items():
                    text = text.replace(placeholder, value)
                return text

            # Apply substitutions to each container's labels dict in-place (both keys and values)
            for c in containers_list:
                if c.get('labels'):
                    c['labels'] = {
                        _apply_substitutions(k): _apply_substitutions(str(v))
                        for k, v in c['labels'].items()
                    }

            # Get config
            from ..models.config import ContainerConfig
            connection_host = ContainerConfig.get('connection_host', 'localhost')

            # Generate names
            safe_name = re.sub(r'[^a-zA-Z0-9-]', '', challenge.name.replace(' ', '-').lower())
            name_prefix = f"{safe_name}-{instance.account_id}"
            network_name = f"ctfd-compose-{instance.uuid[:12]}"

            # Instance-level labels applied to every container
            labels = {
                'ctfd.instance_uuid': instance.uuid,
                'ctfd.challenge_id': str(challenge.id),
                'ctfd.account_id': str(instance.account_id),
                'ctfd.expires_at': str(instance.expires_at.timestamp()),
                'ctfd.managed': 'true',
                'ctfd.plugin': 'containers',
            }

            if use_traefik:
                # --- Traefik path ---
                traefik_container = ContainerConfig.get('traefik_container', 'traefik')

                # Find entry container (optional in pure Traefik mode — may have no expose)
                entry_port = None
                for c in containers_list:
                    if c.get('expose'):
                        entry_port = int(c['expose'])
                        break
                # If no container has 'expose', use a placeholder (tailscale won't run)
                if entry_port is None:
                    entry_port = 80

                result = self.docker.create_container_group(
                    containers_config=containers_list,
                    network_name=network_name,
                    entry_port=entry_port,
                    host_port=None,
                    flag=flag,
                    labels=labels,
                    name_prefix=name_prefix,
                    memory_limit=challenge.get_memory_limit(),
                    cpu_limit=challenge.get_cpu_limit(),
                    pids_limit=challenge.pids_limit,
                    connection_host=connection_host,
                    traefik_container=traefik_container,
                )

                # Extract Traefik URL from container labels (Host(`...`) rule)
                traefik_url = None
                traefik_urls = []
                for c in containers_list:
                    for k, v in c.get('labels', {}).items():
                        if 'rule' in k and 'Host(' in v:
                            import re as _re
                            m = _re.search(r'Host\(`([^`]+)`\)', v)
                            if m:
                                scheme = 'https' if 'websecure' in str(c.get('labels', {})) else 'http'
                                url = f"{scheme}://{m.group(1)}"
                                traefik_urls.append({
                                    'name': c.get('name', ''),
                                    'url': url
                                })
                                if not traefik_url:
                                    traefik_url = url

                instance.container_id = result['entry_container_id']
                instance.container_ids = result['container_ids']
                instance.network_id = result['network_id']
                instance.connection_port = None
                instance.connection_ports = None
                instance.connection_host = connection_host
                instance.connection_info = {
                    'type': challenge.container_connection_type or 'http',
                    'info': challenge.container_connection_info,
                    'compose': True,
                    'traefik': True,
                    'traefik_container': traefik_container,
                    'traefik_url': traefik_url,
                    'traefik_urls': traefik_urls,
                    'containers': len(containers_list),
                }
                host_port = None

            else:
                raise Exception(
                    "Compose mode requires Traefik labels. "
                    "Add 'traefik.enable: true' to at least one container's labels "
                    "or set 'traefik: true' at the top level of compose_config."
                )

            instance.status = 'running'
            instance.started_at = datetime.utcnow()

            db.session.commit()

            # Schedule Redis expiration
            try:
                from .. import redis_expiration_service
                if redis_expiration_service:
                    expires_in = int((instance.expires_at - datetime.utcnow()).total_seconds())
                    redis_expiration_service.schedule_expiration(instance.uuid, expires_in)
            except Exception as e:
                logger.warning(f"Failed to schedule Redis expiration: {e}")

            logger.info(
                f"Provisioned compose group for instance {instance.uuid}: "
                f"{len(containers_list)} containers, network={network_name}, "
                f"{'traefik' if use_traefik else f'port={host_port}'}"
            )

            # Audit log
            self._create_audit_log(
                'instance_started',
                instance_id=instance.id,
                challenge_id=challenge.id,
                account_id=instance.account_id,
                details={
                    'compose': True,
                    'traefik': use_traefik,
                    'containers': len(containers_list),
                    'network': network_name,
                    'port': host_port,
                }
            )

        except Exception as e:
            logger.error(f"Error provisioning compose group: {e}")
            if self.notification_service:
                self.notification_service.notify_error("Compose Provisioning", str(e))
            instance.status = 'error'
            instance.extra_data = {'error': str(e)}
            db.session.commit()
            raise
    
    def renew_instance(self, instance: ContainerInstance, user_id: int) -> ContainerInstance:
        """
        Renew (extend) container expiration
        
        Args:
            instance: ContainerInstance object
            user_id: User requesting renewal
        
        Returns:
            Updated instance
        """
        challenge = ContainerChallenge.query.get(instance.challenge_id)
        
        # Check renewal limit
        max_renewals = challenge.get_max_renewals()
        if instance.renewal_count >= max_renewals:
            raise Exception(f"Maximum renewals ({max_renewals}) reached")
        
        # Extend expiration by 5 minutes (fixed)
        extend_minutes = 5
        instance.extend_expiration(extend_minutes)
        instance.last_accessed_at = datetime.utcnow()
        
        db.session.commit()
        
        # Extend Redis TTL
        try:
            from .. import redis_expiration_service
            if redis_expiration_service:
                redis_expiration_service.extend_expiration(
                    instance.uuid,
                    extend_minutes * 60  # 5 minutes = 300 seconds
                )
        except Exception as e:
            logger.warning(f"Failed to extend Redis expiration: {e}")
        
        # Audit log
        self._create_audit_log(
            'instance_renewed',
            instance_id=instance.id,
            challenge_id=instance.challenge_id,
            account_id=instance.account_id,
            user_id=user_id,
            details={
                'new_expires_at': instance.expires_at.isoformat(),
                'renewal_count': instance.renewal_count
            }
        )
        
        logger.info(f"Renewed instance {instance.uuid} (renewal {instance.renewal_count})")
        
        return instance
    
    def stop_instance(self, instance: ContainerInstance, user_id: int, reason='manual') -> bool:
        """
        Stop container instance
        
        Args:
            instance: ContainerInstance object
            user_id: User stopping the container
            reason: Reason for stopping ('manual', 'expired', 'solved')
        
        Returns:
            True if successful
        """
        if instance.status not in ('running', 'provisioning'):
            return False
        
        instance.status = 'stopping'
        db.session.commit()
        
        # Cancel Redis expiration
        try:
            from .. import redis_expiration_service
            if redis_expiration_service:
                redis_expiration_service.cancel_expiration(instance.uuid)
        except Exception as e:
            logger.warning(f"Failed to cancel Redis expiration: {e}")
        
        try:
            # Stop Docker container(s)
            if instance.container_ids:
                # Multi-container mode: stop all containers + remove network
                compose_info = instance.connection_info or {}
                traefik_container = compose_info.get('traefik_container') if compose_info.get('traefik') else None
                self.docker.stop_container_group(
                    instance.container_ids,
                    instance.network_id,
                    host_port=instance.connection_port,
                    traefik_container=traefik_container,
                )
            elif instance.container_id:
                # Single-container mode
                self.docker.stop_container(instance.container_id)
            
            # Release port back to pool
            if instance.connection_port:
                self.port_manager.release_port(instance.connection_port)
                logger.info(f"Released port {instance.connection_port}")
            
            if instance.connection_ports:
                for int_p, ext_p in instance.connection_ports.items():
                    self.port_manager.release_port(ext_p)
            
            # Update instance based on reason
            if reason == 'solved':
                instance.status = 'solved'
                instance.solved_at = datetime.utcnow()
            else:
                instance.status = 'stopped'
            
            instance.stopped_at = datetime.utcnow()
            
            # Handle flag based on reason (only for random flag mode)
            if reason != 'solved':
                # Get challenge to check flag mode
                challenge = ContainerChallenge.query.get(instance.challenge_id)
                if challenge and challenge.flag_mode == 'random':
                    from ..models.flag import ContainerFlag
                    flag = ContainerFlag.query.filter_by(instance_id=instance.id).first()
                    if flag:
                        # Delete flag instead of invalidating to prevent duplicate hash issues
                        # when user recreates container
                        db.session.delete(flag)
                        logger.info(f"Deleted temporary flag for instance {instance.uuid}")
            
            db.session.commit()
            
            # Audit log
            self._create_audit_log(
                f'instance_stopped_{reason}',
                instance_id=instance.id,
                challenge_id=instance.challenge_id,
                account_id=instance.account_id,
                user_id=user_id,
                details={'reason': reason}
            )
            
            logger.info(f"Stopped instance {instance.uuid} (reason: {reason})")
            
            return True
            
        except Exception as e:
            logger.error(f"Error stopping instance: {e}")
            instance.status = 'error'
            instance.extra_data = {'error': str(e)}
            db.session.commit()
            return False
    
    def cleanup_expired_instances(self):
        """
        Background job: Cleanup expired instances
        
        Optimized for high volume (100+ containers):
        - Prevent overlapping runs
        - Batch processing (max 50 per run)
        - Timeout per container
        - Continue on error
        """
        # Prevent overlapping cleanup jobs
        if self._cleanup_running:
            logger.warning("Cleanup job already running, skipping this run")
            return
        
        self._cleanup_running = True
        
        try:
            import signal
            from contextlib import contextmanager
            
            @contextmanager
            def timeout(seconds):
                """Timeout context manager"""
                def timeout_handler(signum, frame):
                    raise TimeoutError(f"Operation timed out after {seconds}s")
                
                # Set alarm (Unix only)
                old_handler = signal.signal(signal.SIGALRM, timeout_handler)
                signal.alarm(seconds)
                try:
                    yield
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)
            
            # Get expired instances (limit to 50 per run to prevent overload)
            expired = ContainerInstance.query.filter(
                ContainerInstance.status == 'running',
                ContainerInstance.expires_at < datetime.utcnow()
            ).limit(50).all()
            
            if not expired:
                return
            
            logger.warning(f"⚠️ [APSCHEDULER CLEANUP] Found {len(expired)} expired instances (Redis backup cleanup)")
            
            cleaned = 0
            failed = 0
            
            for instance in expired:
                try:
                    # Timeout after 10 seconds per container
                    with timeout(10):
                        logger.warning(f"🟡 [APSCHEDULER KILL] Cleaning up expired instance {instance.uuid}")
                        self.stop_instance(instance, user_id=None, reason='expired')
                        cleaned += 1
                except TimeoutError:
                    logger.error(f"Timeout cleaning up instance {instance.uuid}")
                    # Mark as error so it gets cleaned up later
                    instance.status = 'error'
                    db.session.commit()
                    failed += 1
                except Exception as e:
                    logger.error(f"Error cleaning up instance {instance.uuid}: {e}")
                    failed += 1
            
            logger.info(f"Cleanup completed: {cleaned} cleaned, {failed} failed")
        
        finally:
            self._cleanup_running = False
    
    def cleanup_old_instances(self):
        """
        Background job: Delete old stopped/error instances
        """
        instances = ContainerInstance.query.filter(
            ContainerInstance.status.in_(['stopped', 'error'])
        ).all()
        
        for instance in instances:
            if instance.should_cleanup():
                logger.info(f"Deleting old instance {instance.uuid}")
                try:
                    # Delete associated flags if invalidated
                    from ..models.flag import ContainerFlag
                    ContainerFlag.query.filter_by(
                        instance_id=instance.id,
                        flag_status='invalidated'
                    ).delete()
                    
                    db.session.delete(instance)
                    db.session.commit()
                except Exception as e:
                    logger.error(f"Error deleting instance: {e}")
                    db.session.rollback()
    
    def _create_audit_log(self, event_type, **kwargs):
        """Create audit log entry"""
        log = ContainerAuditLog(
            event_type=event_type,
            ip_address=request.remote_addr if request else None,
            user_agent=request.headers.get('User-Agent') if request else None,
            **kwargs
        )
        db.session.add(log)
