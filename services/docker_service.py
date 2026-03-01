"""
Docker Service - Manage Docker containers
"""
import docker
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class DockerService:
    """
    Service to interact with Docker daemon
    """
    
    def __init__(self, base_url='unix://var/run/docker.sock'):
        """
        Initialize Docker client
        
        Args:
            base_url: Docker daemon URL
                     - Unix socket: 'unix://var/run/docker.sock' (default)
                     - TCP: 'tcp://192.168.1.100:2376'
                     - SSH: 'ssh://user@host:port' or 'ssh://user@host' (default port 22)
        """
        self.base_url = base_url
        self.client = None
        self._connect()
    
    def _connect(self):
        """Connect to Docker daemon - Don't raise exception, just log warning"""
        try:
            # Handle SSH connection
            if self.base_url.startswith('ssh://'):
                logger.info(f"Attempting SSH connection to Docker: {self.base_url}")
                # docker-py supports ssh:// URLs directly
                # Format: ssh://user@host:port or ssh://user@host (default port 22)
                # SSH keys will be used from ~/.ssh/ or SSH agent
                self.client = docker.DockerClient(base_url=self.base_url, timeout=30)
            else:
                # Regular connection (Unix socket or TCP)
                self.client = docker.DockerClient(base_url=self.base_url, timeout=10)
            
            self.client.ping()
            logger.info(f"Connected to Docker daemon at {self.base_url}")
        except Exception as e:
            logger.warning(f"Failed to connect to Docker: {e}")
            logger.warning("Docker connection will be retried when needed. Configure in plugin settings.")
            self.client = None
            # Don't raise - allow plugin to load even if Docker is unavailable
    
    def is_connected(self) -> bool:
        """Check if Docker is connected"""
        if not self.client:
            return False
        try:
            self.client.ping()
            return True
        except:
            return False
    
    def create_container(
        self,
        image: str,
        internal_port: int = None,
        host_port: int = None,
        ports: Dict[str, int] = None,  # New: {'80': 30001, '22': 30002}
        command: str = None,
        environment: Dict[str, str] = None,
        memory_limit: str = "512m",
        cpu_limit: float = 0.5,
        pids_limit: int = 100,
        labels: Dict[str, str] = None,
        name: str = None,
        network: str = None,  # Network to connect for Traefik routing
        use_traefik: bool = False,  # If True, don't expose host port (Traefik handles routing)
        network_mode: str = None  # Share network with another container (e.g. 'container:tailscale-challenges')
    ) -> Dict[str, Any]:
        """
        Create and start a container
        
        Args:
            image: Docker image name
            internal_port: Port inside container
            host_port: Port on host to expose (ignored if use_traefik=True or network_mode is set)
            command: Command to run
            environment: Environment variables
            memory_limit: Memory limit (e.g., "512m", "1g")
            cpu_limit: CPU limit (0.5 = 50% of one core)
            pids_limit: Max number of processes
            labels: Labels for the container
            name: Container name (optional)
            network: Docker network to connect (for Traefik routing)
            use_traefik: If True, use Traefik for routing instead of host port
            network_mode: If set, share network stack with another container.
                         Cannot be used together with 'network' or 'ports'.
                         Example: 'container:tailscale-challenges'
        
        Returns:
            {
                'container_id': str,
                'status': str,
                'port': int
            }
        """
        if not self.is_connected():
            raise Exception("Docker is not connected")
        
        try:
            # CPU quota calculation
            cpu_period = 100000  # Docker default
            cpu_quota = int(cpu_limit * cpu_period)
            
            # Labels for management
            container_labels = labels or {}
            container_labels.update({
                'ctfd.managed': 'true',
                'ctfd.plugin': 'containers'
            })
            
            # Network mode: share network stack with another container (e.g. tailscale)
            # When using network_mode, Docker does NOT allow ports or network args
            if network_mode:
                container = self.client.containers.run(
                    image=image,
                    name=name,
                    command=command,
                    detach=True,
                    auto_remove=True,
                    environment=environment or {},
                    mem_limit=memory_limit,
                    cpu_quota=cpu_quota,
                    cpu_period=cpu_period,
                    pids_limit=pids_limit,
                    labels=container_labels,
                    network_mode=network_mode,
                    # Security options
                    cap_drop=['ALL'],
                    cap_add=['CHOWN', 'SETUID', 'SETGID'],
                    security_opt=['no-new-privileges'],
                )
            else:
                # Port mapping - only if not using Traefik
                ports_config = None
                if not use_traefik:
                    if ports:
                        # New multi-port mode: ports = {'80': 30001, '22': 30002}
                        ports_config = {}
                        for internal, external in ports.items():
                            ports_config[f'{internal}/tcp'] = external
                    else:
                        # Legacy single port mode
                        ports_config = {f'{internal_port}/tcp': host_port}
                
                # Network configuration
                network_arg = network if network else 'bridge'
                
                # Create container
                container = self.client.containers.run(
                    image=image,
                    name=name,
                    command=command,
                    detach=True,
                    auto_remove=True,
                    ports=ports_config,
                    environment=environment or {},
                    mem_limit=memory_limit,
                    cpu_quota=cpu_quota,
                    cpu_period=cpu_period,
                    pids_limit=pids_limit,
                    labels=container_labels,
                    network=network_arg,
                    # Security options
                    cap_drop=['ALL'],
                    cap_add=['CHOWN', 'SETUID', 'SETGID'],
                    security_opt=['no-new-privileges'],
                )
            
            # No need to manually connect if network arg is used
            logger.info(f"Created container {container.id[:12]} from image {image}")
            
            return {
                'container_id': container.id,
                'status': container.status,
                'port': host_port
            }
            
        except docker.errors.ImageNotFound:
            logger.error(f"Docker image not found: {image}")
            raise Exception(f"Docker image '{image}' not found")
        except docker.errors.APIError as e:
            logger.error(f"Docker API error: {e}")
            raise Exception(f"Failed to create container: {e}")
        except Exception as e:
            logger.error(f"Unexpected error creating container: {e}")
            raise
    
    def stop_container(self, container_id: str) -> bool:
        """
        Stop and remove a container
        
        Args:
            container_id: Container ID
        
        Returns:
            True if successful, False otherwise
        """
        if not self.is_connected():
            logger.warning("Docker not connected, cannot stop container")
            return False
        
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=3)
            container.remove()
            logger.info(f"Stopped and removed container {container_id[:12]}")
            return True
        except docker.errors.NotFound:
            logger.info(f"Container {container_id[:12]} not found (already removed)")
            return True
        except Exception as e:
            logger.error(f"Error stopping container {container_id[:12]}: {e}")
            return False
    
    def get_container_status(self, container_id: str) -> Optional[str]:
        """
        Get container status
        
        Returns:
            Status string ('running', 'exited', etc.) or None if not found
        """
        if not self.is_connected():
            return None
        
        try:
            container = self.client.containers.get(container_id)
            return container.status
        except docker.errors.NotFound:
            return None
        except Exception as e:
            logger.error(f"Error getting container status: {e}")
            return None
    
    def is_container_running(self, container_id: str) -> bool:
        """Check if container is running"""
        status = self.get_container_status(container_id)
        return status == 'running'
    
    def list_managed_containers(self):
        """
        List all containers managed by this plugin
        
        Returns:
            List of container objects
        """
        if not self.is_connected():
            return []
        
        try:
            return self.client.containers.list(
                all=True,
                filters={'label': 'ctfd.managed=true'}
            )
        except Exception as e:
            logger.error(f"Error listing containers: {e}")
            return []
    
    def list_images(self):
        """
        List all available Docker images
        
        Returns:
            List of image objects
        """
        if not self.is_connected():
            raise Exception("Docker is not connected")
        
        try:
            images = self.client.images.list()
            return images
        except Exception as e:
            logger.error(f"Failed to list images: {e}")
            raise Exception(f"Failed to list Docker images: {e}")
    
    def get_container_logs(self, container_id: str, tail: int = 100) -> Optional[str]:
        """
        Get container logs
        
        Args:
            container_id: Container ID
            tail: Number of lines to return
        
        Returns:
            Logs as string or None
        """
        if not self.is_connected():
            return None
        
        try:
            container = self.client.containers.get(container_id)
            logs = container.logs(tail=tail).decode('utf-8', errors='ignore')
            return logs
        except Exception as e:
            logger.error(f"Error getting container logs: {e}")
            return None
    
    def cleanup_expired_containers(self, instance_uuids: list):
        """
        Cleanup containers không còn trong database
        
        Args:
            instance_uuids: List of valid instance UUIDs from database
        """
        if not self.is_connected():
            return
        
        try:
            containers = self.list_managed_containers()
            for container in containers:
                instance_uuid = container.labels.get('ctfd.instance_uuid')
                if instance_uuid and instance_uuid not in instance_uuids:
                    logger.info(f"Cleaning up orphaned container {container.id[:12]}")
                    try:
                        container.stop(timeout=5)
                        container.remove()
                    except:
                        pass
        except Exception as e:
            logger.error(f"Error during cleanup: {e}")

    def create_network(self, name: str, internal: bool = False, driver: str = 'bridge', options: Dict[str, str] = None) -> bool:
        """
        Create a Docker network
        
        Args:
            name: Network name
            internal: If True, restrict external access
            driver: Network driver (default: bridge)
            options: Driver options (e.g. {'com.docker.network.bridge.enable_icc': 'false'})
        
        Returns:
            True if created or already exists, False on error
        """
        if not self.is_connected():
            return False
            
        try:
            # Check if exists
            try:
                self.client.networks.get(name)
                return True
            except docker.errors.NotFound:
                pass
                
            self.client.networks.create(
                name=name,
                driver=driver,
                internal=internal,
                options=options,
                check_duplicate=True,
                labels={'ctfd.managed': 'true'}
            )
            logger.info(f"Created network {name}")
            return True
        except Exception as e:
            logger.error(f"Failed to create network {name}: {e}")
            return False

    def remove_network(self, name: str) -> bool:
        """
        Remove a Docker network
        
        Args:
            name: Network name
        
        Returns:
            True if removed or not found, False on error
        """
        if not self.is_connected():
            return False
            
        try:
            network = self.client.networks.get(name)
            network.remove()
            logger.info(f"Removed network {name}")
            return True
        except docker.errors.NotFound:
            return True
        except Exception as e:
            # Often fails if network is in use, which is expected during race conditions
            logger.warning(f"Failed to remove network {name} (might be in use): {e}")
            return False

    def create_container_group(
        self,
        containers_config: list,
        network_name: str,
        entry_port: int,
        host_port: int,
        flag: str,
        labels: Dict[str, str] = None,
        name_prefix: str = '',
        memory_limit: str = '512m',
        cpu_limit: float = 0.5,
        pids_limit: int = 100,
        connection_host: str = 'localhost',
        tailnet_container: str = 'tailscale-challenges',
    ) -> Dict[str, Any]:
        """
        Create a group of containers with a private network (compose-like).
        
        All containers connect to a private bridge network for inter-container DNS.
        Entry container is exposed via tailscale serve — no host port mapping needed.
        
        Flow:
        1. Create private bridge network
        2. Create all containers on bridge
        3. Connect tailscale-challenges to the bridge
        4. Run 'tailscale serve --bg --tcp PORT tcp://entry:port' inside tailscale-challenges
        
        Result: challenges.ts.bksec.vn:PORT → entry container (tailnet only, no public exposure)
        """
        if not self.is_connected():
            raise Exception("Docker is not connected")
        
        created_containers = []
        network = None
        ts_connected = False
        ts_serve_port = None
        
        try:
            # 1. Create private bridge network
            network = self.client.networks.create(
                name=network_name,
                driver='bridge',
                labels={
                    'ctfd.managed': 'true',
                    'ctfd.compose_group': name_prefix,
                    **(labels or {})
                }
            )
            logger.info(f"Created private network: {network_name}")
            
            cpu_period = 100000
            cpu_quota = int(cpu_limit * cpu_period)
            
            # 2. Find entry container (the one with 'expose')
            entry_idx = None
            for i, c in enumerate(containers_config):
                if c.get('expose'):
                    entry_idx = i
                    break
            
            if entry_idx is None:
                raise Exception("No container has 'expose' defined. One container must expose a port.")
            
            # 3. Create non-entry containers (on private network only)
            for i, c_def in enumerate(containers_config):
                if i == entry_idx:
                    continue
                
                c_name = f"{name_prefix}-{c_def['name']}"
                c_env = dict(c_def.get('environment', {}))
                c_labels = dict(labels or {})
                c_labels['ctfd.compose_role'] = 'internal'
                c_labels['ctfd.compose_name'] = c_def['name']
                
                container = self.client.containers.run(
                    image=c_def['image'],
                    name=c_name,
                    command=c_def.get('command'),
                    detach=True,
                    auto_remove=True,
                    environment=c_env,
                    mem_limit=memory_limit,
                    cpu_quota=cpu_quota,
                    cpu_period=cpu_period,
                    pids_limit=pids_limit,
                    labels=c_labels,
                    networking_config=self.client.api.create_networking_config({
                        network_name: self.client.api.create_endpoint_config(
                            aliases=[c_def['name']]
                        )
                    }),
                )
                created_containers.append(container)
                logger.info(f"Created internal container: {c_name} ({container.id[:12]})")
            
            # 4. Create entry container on private network (NO port mapping)
            entry_def = containers_config[entry_idx]
            entry_name = f"{name_prefix}-{entry_def['name']}"
            entry_env = dict(entry_def.get('environment', {}))
            entry_env['FLAG'] = flag
            entry_env['PORT'] = str(entry_port)
            
            entry_labels = dict(labels or {})
            entry_labels['ctfd.compose_role'] = 'entry'
            entry_labels['ctfd.compose_name'] = entry_def['name']
            
            entry_container = self.client.containers.run(
                image=entry_def['image'],
                name=entry_name,
                command=entry_def.get('command'),
                detach=True,
                auto_remove=True,
                environment=entry_env,
                mem_limit=memory_limit,
                cpu_quota=cpu_quota,
                cpu_period=cpu_period,
                pids_limit=pids_limit,
                labels=entry_labels,
                networking_config=self.client.api.create_networking_config({
                    network_name: self.client.api.create_endpoint_config(
                        aliases=[entry_def['name']]
                    )
                }),
            )
            created_containers.append(entry_container)
            logger.info(f"Created entry container: {entry_name} ({entry_container.id[:12]})")
            
            # 5. Connect tailscale-challenges to the private bridge network
            ts_container = self.client.containers.get(tailnet_container)
            network.connect(ts_container)
            ts_connected = True
            logger.info(f"Connected {tailnet_container} to {network_name}")
            
            # 6. Set up tailscale serve to forward tailnet traffic to entry container
            serve_cmd = f'tailscale serve --bg --tcp {host_port} tcp://{entry_name}:{entry_port}'
            exit_code, output = ts_container.exec_run(serve_cmd)
            if exit_code != 0:
                raise Exception(f"tailscale serve failed (exit {exit_code}): {output.decode()}")
            ts_serve_port = host_port
            logger.info(f"Tailscale serve: port {host_port} -> {entry_name}:{entry_port}")
            logger.info(f"Output: {output.decode().strip()}")
            
            all_ids = [c.id for c in created_containers]
            
            return {
                'container_ids': all_ids,
                'entry_container_id': entry_container.id,
                'network_id': network.id,
                'port': host_port,
            }
            
        except Exception as e:
            logger.error(f"Error creating container group: {e}")
            # Cleanup tailscale serve
            if ts_serve_port:
                try:
                    ts_container = self.client.containers.get(tailnet_container)
                    ts_container.exec_run(f'tailscale serve --tcp {ts_serve_port} off')
                except:
                    pass
            # Disconnect tailscale from network
            if ts_connected and network:
                try:
                    ts_container = self.client.containers.get(tailnet_container)
                    network.disconnect(ts_container)
                except:
                    pass
            # Stop containers
            for c in created_containers:
                try:
                    c.stop(timeout=3)
                    c.remove()
                except:
                    pass
            if network:
                try:
                    network.remove()
                except:
                    pass
            raise
    
    def stop_container_group(
        self, 
        container_ids: list, 
        network_id: str = None,
        host_port: int = None,
        tailnet_container: str = 'tailscale-challenges',
    ) -> bool:
        """
        Stop all containers in a group, clean up tailscale serve, and remove the private network.
        """
        if not self.is_connected():
            logger.warning("Docker not connected, cannot stop container group")
            return False
        
        success = True
        
        # 1. Remove tailscale serve forwarding
        if host_port:
            try:
                ts_container = self.client.containers.get(tailnet_container)
                ts_container.exec_run(f'tailscale serve --tcp {host_port} off')
                logger.info(f"Removed tailscale serve for port {host_port}")
            except docker.errors.NotFound:
                logger.warning(f"Tailscale container {tailnet_container} not found")
            except Exception as e:
                logger.warning(f"Error removing tailscale serve: {e}")
        
        # 2. Stop containers
        for cid in (container_ids or []):
            try:
                container = self.client.containers.get(cid)
                container.stop(timeout=3)
                container.remove()
                logger.info(f"Stopped container {cid[:12]}")
            except docker.errors.NotFound:
                logger.info(f"Container {cid[:12]} not found (already removed)")
            except Exception as e:
                logger.error(f"Error stopping container {cid[:12]}: {e}")
                success = False
        
        # 3. Disconnect tailscale-challenges from network & remove network
        if network_id:
            try:
                network = self.client.networks.get(network_id)
                # Disconnect tailscale-challenges first
                try:
                    ts_container = self.client.containers.get(tailnet_container)
                    network.disconnect(ts_container)
                    logger.info(f"Disconnected {tailnet_container} from network {network_id[:12]}")
                except:
                    pass
                network.remove()
                logger.info(f"Removed private network {network_id[:12]}")
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.warning(f"Failed to remove network {network_id[:12]}: {e}")
                success = False
        
        return success
