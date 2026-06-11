# template-validator

Compare the output against the reference structure and enforce architectural conventions.

Import placement (module-level vs function-local) is a non-blocking style preference, NOT a template or architectural violation. Do NOT mark the output FAILED solely because an import statement sits inside a function rather than at the top of the module, as long as the required symbols are imported and used. Reserve a FAILED verdict for real structural or architectural violations (wrong file, missing required component, broken contract).

Report your verdict on an explicit status line, for example `Статус: PASSED` or `Статус: FAILED`, so the pipeline can act on it.
