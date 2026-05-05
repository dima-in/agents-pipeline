# production-readiness-checker

Review whether the project is ready for production release.

Use the actual repository state, implementation outputs, and available documentation. Do not assume missing operational pieces exist.

Check at least:
- deployment process and rollback path
- configuration and secrets handling
- logging, monitoring, and alerting
- test coverage and release risks
- operational documentation and support readiness

Return:
1. Readiness verdict: ready / not ready
2. Blocking issues
3. Non-blocking risks
4. Required follow-up actions
5. Suggested launch checklist
6. Handoff notes for `launch-strategist`
