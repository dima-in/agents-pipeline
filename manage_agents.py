from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import yaml
from colorama import Fore, Style, init

init(autoreset=True)


class AgentManager:
    def __init__(self, base_dir: str = '.openclaw/agents', config_dir: str = '.openclaw/config') -> None:
        self.base_dir = Path(base_dir)
        self.config_dir = Path(config_dir)
        self.settings = self._load_yaml(self.config_dir / 'settings.yaml', {})
        self.agent_catalog = self._load_yaml(self.config_dir / 'agents.yaml', {'agents': []})
        self.openclaw_bin = self.settings.get('openclaw', {}).get('bin', 'openclaw')
        self.workspace = self.settings.get('project', {}).get('workspace', '.')
        self.default_timeout = self.settings.get('agents', {}).get('default_timeout', 600)

    def list_agents(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        if not self.base_dir.exists():
            return result

        for phase_dir in sorted(p for p in self.base_dir.iterdir() if p.is_dir()):
            result[phase_dir.name] = sorted(
                agent_dir.name for agent_dir in phase_dir.iterdir() if agent_dir.is_dir()
            )
        return result

    def print_agents(self) -> None:
        print(f'{Fore.CYAN}Agents{Style.RESET_ALL}')
        for phase, agents in self.list_agents().items():
            print(f'{Fore.YELLOW}{phase}{Style.RESET_ALL}')
            for agent in agents:
                print(f'  - {agent}')

    def create_agent(
        self,
        name: str,
        phase: str,
        description: str = '',
        timeout: int | None = None,
        force: bool = False,
    ) -> bool:
        agent_dir = self.base_dir / phase / name
        if agent_dir.exists() and not force:
            print(f'{Fore.RED}Agent already exists: {agent_dir}{Style.RESET_ALL}')
            return False

        agent_dir.mkdir(parents=True, exist_ok=True)
        resolved_timeout = timeout or self.default_timeout
        (agent_dir / 'config.yaml').write_text(
            '\n'.join(
                [
                    f'name: {name}',
                    f'phase: "{phase}"',
                    f'description: "{description}"',
                    f'timeout: {resolved_timeout}',
                    'commands:',
                    f'  - echo "Run {name}"',
                    '',
                ]
            ),
            encoding='utf-8',
        )
        (agent_dir / 'prompt.md').write_text(
            f'# {name}\n\n{description or "Agent prompt placeholder."}\n',
            encoding='utf-8',
        )
        return True

    def delete_agent(self, name: str, phase: str) -> bool:
        agent_dir = self.base_dir / phase / name
        if not agent_dir.exists():
            print(f'{Fore.RED}Agent not found: {agent_dir}{Style.RESET_ALL}')
            return False
        shutil.rmtree(agent_dir)
        return True

    def register_agent(self, name: str, phase: str) -> int:
        agent_dir = self.base_dir / phase / name
        if not agent_dir.exists():
            print(f'{Fore.RED}Agent not found: {agent_dir}{Style.RESET_ALL}')
            return 1

        cmd = [
            self.openclaw_bin,
            'agents',
            'add',
            name,
            '--agent-dir',
            str(agent_dir),
            '--workspace',
            self.workspace,
        ]
        return self._run_command(cmd)

    def register_all(self) -> int:
        exit_code = 0
        for phase, agents in self.list_agents().items():
            for agent in agents:
                code = self.register_agent(agent, phase)
                exit_code = exit_code or code
        return exit_code

    def test_agent(self, name: str, phase: str) -> int:
        agent_dir = self.base_dir / phase / name
        if not agent_dir.exists():
            print(f'{Fore.RED}Agent not found: {agent_dir}{Style.RESET_ALL}')
            return 1

        cmd = [self.openclaw_bin, 'run', name, '--agent-dir', str(agent_dir), '--workspace', self.workspace]
        return self._run_command(cmd)

    def bootstrap_defaults(self, force: bool = False) -> int:
        created = 0
        for spec in self.agent_catalog.get('agents', []):
            ok = self.create_agent(
                spec['name'],
                spec['phase'],
                spec.get('description', ''),
                spec.get('timeout', self.default_timeout),
                force=force,
            )
            if ok:
                created += 1
        print(f'{Fore.GREEN}Bootstrapped {created} agent entries{Style.RESET_ALL}')
        return 0

    @staticmethod
    def _load_yaml(path: Path, fallback: dict) -> dict:
        if not path.exists():
            return fallback
        with path.open('r', encoding='utf-8') as handle:
            return yaml.safe_load(handle) or fallback

    @staticmethod
    def _run_command(cmd: list[str]) -> int:
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        except FileNotFoundError:
            print(f'{Fore.RED}Command not found: {cmd[0]}{Style.RESET_ALL}')
            return 1

        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='Manage agents-pipeline agents')
    subparsers = parser.add_subparsers(dest='command')

    subparsers.add_parser('list')

    create_parser = subparsers.add_parser('create')
    create_parser.add_argument('name')
    create_parser.add_argument('phase', choices=['research', 'implementation'])
    create_parser.add_argument('--description', default='')
    create_parser.add_argument('--timeout', type=int, default=None)
    create_parser.add_argument('--force', action='store_true')

    delete_parser = subparsers.add_parser('delete')
    delete_parser.add_argument('name')
    delete_parser.add_argument('phase', choices=['research', 'implementation'])

    test_parser = subparsers.add_parser('test')
    test_parser.add_argument('name')
    test_parser.add_argument('phase', choices=['research', 'implementation'])

    register_parser = subparsers.add_parser('register')
    register_parser.add_argument('name')
    register_parser.add_argument('phase', choices=['research', 'implementation'])

    subparsers.add_parser('register-all')

    bootstrap_parser = subparsers.add_parser('bootstrap')
    bootstrap_parser.add_argument('--force', action='store_true')

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    manager = AgentManager()

    if args.command == 'list':
        manager.print_agents()
        return 0
    if args.command == 'create':
        return 0 if manager.create_agent(args.name, args.phase, args.description, args.timeout, args.force) else 1
    if args.command == 'delete':
        return 0 if manager.delete_agent(args.name, args.phase) else 1
    if args.command == 'test':
        return manager.test_agent(args.name, args.phase)
    if args.command == 'register':
        return manager.register_agent(args.name, args.phase)
    if args.command == 'register-all':
        return manager.register_all()
    if args.command == 'bootstrap':
        return manager.bootstrap_defaults(args.force)

    parser.print_help()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
