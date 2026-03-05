"""
Vulnerable web app - intentional command injection via ping endpoint.
Used as CTF challenge: player exploits RCE to reach internal 'target' container.
"""
import subprocess
from flask import Flask, request, render_template_string

app = Flask(__name__)

PAGE = """
<!DOCTYPE html>
<html>
<head><title>Network Diagnostic Tool</title>
<style>
  body { font-family: monospace; background: #1a1a1a; color: #00ff41; padding: 40px; }
  h1 { color: #00ff41; }
  input { background: #000; color: #00ff41; border: 1px solid #00ff41; padding: 8px; width: 300px; }
  button { background: #00ff41; color: #000; border: none; padding: 8px 16px; cursor: pointer; font-weight: bold; }
  pre { background: #000; border: 1px solid #333; padding: 16px; white-space: pre-wrap; word-break: break-all; }
</style>
</head>
<body>
  <h1>Network Diagnostic Tool v1.0</h1>
  <p>Enter a hostname or IP to ping:</p>
  <form method="GET" action="/ping">
    <input name="host" value="{{ host }}" placeholder="e.g. 8.8.8.8">
    <button type="submit">Ping</button>
  </form>
  {% if output %}
  <h3>Output:</h3>
  <pre>{{ output }}</pre>
  {% endif %}
</body>
</html>
"""

@app.route('/')
def index():
    return render_template_string(PAGE, host='', output=None)

@app.route('/ping')
def ping():
    host = request.args.get('host', '')
    if not host:
        return render_template_string(PAGE, host='', output='No host provided.')
    try:
        # Intentionally vulnerable: user input passed directly to shell
        result = subprocess.check_output(
            f'ping -c 2 {host}',
            shell=True,
            stderr=subprocess.STDOUT,
            timeout=10
        )
        output = result.decode('utf-8', errors='replace')
    except subprocess.CalledProcessError as e:
        output = e.output.decode('utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        output = 'Command timed out.'
    except Exception as e:
        output = str(e)
    return render_template_string(PAGE, host=host, output=output)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
