# Project Laddu — Installation Contract

Use `INSTALL_UPDATE.cmd` from a fully extracted package and approve Administrator elevation.

The installer supports a clean Windows machine or an existing Project Laddu installation. Before any Laddu runtime stop or target mutation it validates package integrity and source lineage, checks Windows prerequisites, bootstraps supported missing prerequisites with Windows Package Manager where possible, validates pinned Python environments, proves existing data-plane migration lineage, and captures authority-retention evidence.

Default installed application/state root: `C:\ProgramData\ProjectLaddu`.
Installer and diagnostic evidence: `C:\Temp\ProjectLaddu`.

Missing prerequisites that require a reboot, BIOS virtualization change, corporate-policy approval, or an unavailable package manager stop the installation before Project Laddu target mutation. After reboot or policy remediation, rerun the same `INSTALL_UPDATE.cmd`.

Installer path policy: `C:\ProgramData\ProjectLaddu` owns all installed/runtime and transactional installer working state. `C:\Temp\ProjectLaddu` is reserved for logs, diagnostics, and evidence exports only.
