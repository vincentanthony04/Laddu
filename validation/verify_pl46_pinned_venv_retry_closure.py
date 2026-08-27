#!/usr/bin/env python3
from pathlib import Path
import json,re
ROOT=Path(__file__).resolve().parents[1]
install=(ROOT/'installer/install.ps1').read_text(encoding='utf-8-sig')
auth=(ROOT/'installer/authority_retention_gate.ps1').read_text(encoding='utf-8-sig')
checks={
 'resolver_defined':'function Resolve-PinnedPythonRuntime' in auth,
 'test_defined':'function Test-PinnedPythonEnvironment' in auth,
 'exact_hash_reuse':"$hashTag = $requirementsHash.Substring(0,12)" in auth and "-python-*-" in auth,
 'prior_env_not_mutated':'will not be mutated' in auth and 'pip install --disable-pip-version-check -r $RequirementsPath' in auth,
 'poisoned_retry_recovery':"-recovery-" in auth and 'Creating exact isolated dependency environment' in auth,
 'python_version_guard':'candidateVersion' in auth and 'baseVersion' in auth,
 'backend_uses_resolver':"-Family 'backend'" in install,
 'research_uses_resolver':"-Family 'research'" in install,
 'old_verify_only_branch_removed':'Existing release-isolated research environment found; verifying exact pins without modifying it' not in install,
 'fail_closed_assert':'throw "Pinned Python environment does not match $RequirementsPath"' in auth,
}
failed=[k for k,v in checks.items() if not v]
print(json.dumps({'ok':not failed,'checks':checks,'failures':failed,'broker_authority':'NONE'},indent=2))
raise SystemExit(0 if not failed else 2)
