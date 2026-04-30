# agents-pipeline

`agents-pipeline` is a Windows-first scaffold for a portable multi-agent development workflow.

It is designed to live in a normal Git repository so you can:
- keep the system itself in GitHub
- clone it to several Windows devices
- install dependencies in a repeatable way
- create and register agents locally
- use the same repo to work on other projects

## What goes into Git

Keep in the repository:
- Python source
- workflow config
- agent configs and prompts
- tests
- documentation
- `.env.example`

Do not commit:
- `venv/`
- `.openclaw/logs/`
- `.openclaw/feedback/`
- `.env`
- local caches

## Structure

- `.openclaw/agents/` agent prompts and configs
- `.openclaw/config/` local agent and tool settings
- `.openclaw/feedback/` QA notes and retry feedback
- `.openclaw/logs/` text and JSON session logs
- `workflow/` orchestration logic
- `tests/` smoke tests

## First setup on a new Windows device

1. Clone the repository:

```powershell
git clone <YOUR_GITHUB_URL>
cd agents-pipeline
```

2. Create local environment file:

```powershell
Copy-Item .env.example .env
```

3. Install dependencies:

```powershell
.\install.bat
```

4. Bootstrap default agents:

```powershell
.\run.bat python manage_agents.py bootstrap
```

5. If `openclaw` is installed and available, register agents:

```powershell
.\run.bat python manage_agents.py register-all
```

6. Start the workflow:

```powershell
.\run.bat --mode interactive
```

## Daily workflow across three Windows devices

1. `git pull`
2. work locally
3. `git add .`
4. `git commit -m "..."`
5. `git push`
6. on another device: `git pull`

## Agent management

List agents:

```powershell
.\run.bat python manage_agents.py list
```

Create a new agent:

```powershell
.\run.bat python manage_agents.py create ux-reviewer research --description "Review UX and competitor patterns"
```

Register one agent in `openclaw`:

```powershell
.\run.bat python manage_agents.py register ux-reviewer research
```

Rebuild all default agents from config:

```powershell
.\run.bat python manage_agents.py bootstrap --force
```

## Notes

This project was reconstructed from a streamed chat draft. Broken or contradictory fragments were normalized into a coherent baseline rather than copied verbatim.
