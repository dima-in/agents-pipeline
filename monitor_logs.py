from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from colorama import Fore, Style, init

init(autoreset=True)


class LogMonitor:
    def __init__(self, log_dir: str = ".openclaw/logs") -> None:
        self.log_dir = Path(log_dir)

    def get_latest_log(self) -> Path | None:
        files = list(self.log_dir.glob("workflow_*.log"))
        return max(files, key=lambda p: p.stat().st_mtime) if files else None

    def get_latest_json_log(self) -> Path | None:
        files = list(self.log_dir.glob("workflow_*.json"))
        return max(files, key=lambda p: p.stat().st_mtime) if files else None

    def monitor_text_log(self) -> None:
        log_file = self.get_latest_log()
        if not log_file:
            print(f"{Fore.RED}No text logs found{Style.RESET_ALL}")
            return

        print(f"{Fore.CYAN}Watching {log_file}{Style.RESET_ALL}")
        with log_file.open("r", encoding="utf-8") as handle:
            handle.seek(0, 2)
            while True:
                line = handle.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                self._print_colored(line.rstrip())

    def show_json_summary(self) -> None:
        json_log = self.get_latest_json_log()
        if not json_log:
            print(f"{Fore.RED}No JSON logs found{Style.RESET_ALL}")
            return

        events = json.loads(json_log.read_text(encoding="utf-8"))
        print(f"{Fore.CYAN}Summary for {json_log.name}{Style.RESET_ALL}")
        for event in events:
            if event["type"] in {"phase_start", "phase_end", "agent_end", "error"}:
                print(f"- {event['timestamp']} | {event['type']} | {event['data']}")

    @staticmethod
    def _print_colored(line: str) -> None:
        if "ERROR" in line:
            print(Fore.RED + line)
        elif "WARNING" in line:
            print(Fore.YELLOW + line)
        elif "PHASE_" in line:
            print(Fore.CYAN + Style.BRIGHT + line)
        elif "AGENT_" in line:
            print(Fore.MAGENTA + line)
        else:
            print(line)


def main() -> int:
    monitor = LogMonitor()
    if len(sys.argv) > 1 and sys.argv[1] == "summary":
        monitor.show_json_summary()
        return 0

    try:
        monitor.monitor_text_log()
    except KeyboardInterrupt:
        print(f"\n{Fore.YELLOW}Monitoring stopped{Style.RESET_ALL}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
