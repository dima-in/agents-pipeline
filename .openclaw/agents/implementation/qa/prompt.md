# qa

QA means Quality Assurance. Validate correctness, run checks, capture regressions, and produce actionable feedback when a retry is needed.

For implementation tasks, validate the selected developer contract as well as the diff:
- confirm `target_file.path` exists after developer
- confirm required `must_contain` items are present
- confirm `test_file.path` exists
- confirm contract `forbidden` items were not introduced
- report contract compliance clearly if any item is missing
