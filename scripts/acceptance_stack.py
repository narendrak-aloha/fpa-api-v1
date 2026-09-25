"""Generate an isolated acceptance stack; never reuses the developer's volumes.

Usage: python scripts/acceptance_stack.py > /tmp/fpa-acceptance.yml
       docker compose -f /tmp/fpa-acceptance.yml up --build -d
"""
from pathlib import Path
import yaml

root = Path(__file__).resolve().parents[1]
config = yaml.safe_load((root / 'docker/docker-compose.yml').read_text())
config['name'] = 'fpa-acceptance'
ports = {'fpa-dev': ['18000:8000'], 'postgres': ['15431:5432'],
         'clickhouse': ['18123:8123'], 'temporal': ['17233:7233', '18233:8233'],
         'commitment': ['18100:8100']}
for name, service in config['services'].items():
    service.pop('container_name', None)
    service['ports'] = ports.get(name, [])
    if service.get('image') == 'fpa-dev':
        service['image'] = 'fpa-acceptance-app'
        service['volumes'] = [f'{root}:/app']
    if 'build' in service:
        service['build']['context'] = str(root)
    environment = service.get('environment', {})
    for key in ('ANTHROPIC_API_KEY', 'GOOGLE_API_KEY'):
        if key in environment:
            environment[key] = ''
    if name == 'fpa-dev':
        environment['FPA_SKIP_SEED'] = '0'
print(yaml.safe_dump(config, sort_keys=False))
