"""Verification script generator for DrSpec contracts.

This module generates executable Python verification scripts from contracts.
These scripts can verify that a function's behavior matches its contract.
"""

from __future__ import annotations

import hashlib
import re
from typing import Optional

from drspec.contracts.schema import Contract, Invariant


def generate_verification_script(contract: Contract, function_id: str) -> str:
    """Generate a verification script from a contract.

    The generated script contains a verify() function that checks all
    invariants against provided input/output data.

    Args:
        contract: The contract to generate a script for.
        function_id: The function ID for documentation purposes.

    Returns:
        Python source code as a string.
    """
    lines = [repr(f"Verification script for: {function_id}\nContract Intent: {contract.intent_summary}"),
             "from __future__ import annotations", "DRSPEC_VERIFIER_VERSION = 2", ""]
    for i, invariant in enumerate(contract.invariants, 1):
        lines.extend(_generate_invariant_check(i, invariant))
    lines.extend(_generate_verify_function(contract.invariants))
    return "\n".join(lines)


def _generate_invariant_check(index: int, invariant: Invariant) -> list[str]:
    expression = _translate_invariant_logic(invariant.logic, invariant.name)
    return [f"def _check_invariant_{index}(input_data, output_data):",
            f"    return {expression}" if expression else "    return None", ""]


def _generate_verify_function(invariants: list[Invariant]) -> list[str]:
    lines = ["def verify(input_data, output_data):", "    results = []"]
    for i, invariant in enumerate(invariants, 1):
        lines += [f"    row = {{'name': {invariant.name!r}, 'logic': {invariant.logic!r}, 'criticality': {invariant.criticality.value!r}}}",
                  "    try:", f"        checked = _check_invariant_{i}(input_data, output_data)",
                  "        row['status'] = 'skipped' if checked is None else ('passed' if checked else 'failed')",
                  "        row['message'] = 'Unsupported rule; manual verification required' if checked is None else row['status']",
                  "    except Exception as exc:", "        row.update(status='error', message=str(exc))",
                  "    results.append(row)"]
    lines += ["    counts = {state: sum(r['status'] == state for r in results) for state in ('passed', 'failed', 'skipped', 'error')}",
              "    status = ('error' if counts['error'] else 'failed' if counts['failed'] else 'partial' if counts['skipped'] and counts['passed'] else 'unverified' if counts['skipped'] or not results else 'passed')",
              "    return dict(status=status, passed=status == 'passed', results=results,",
              "                message=', '.join(f'{n} {state}' for state, n in counts.items()) + '; ' + ', '.join(r['name'] for r in results if r['status'] in ('failed', 'error')),",
              "                invariants_checked=len(results) - counts['skipped'], invariants_passed=counts['passed'],",
              "                invariants_failed=counts['failed'], invariants_skipped=counts['skipped'], invariants_error=counts['error'])", ""]
    return lines


def _escape_string(s: str) -> str:
    """Escape backslashes, quotes, and newlines for embedding in generated code."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _translate_invariant_logic(logic: str, name: str) -> Optional[str]:
    """Attempt to translate natural language invariant to Python code.

    This is a best-effort translation that handles common patterns.
    Returns None if the invariant cannot be automatically translated.

    Args:
        logic: Natural language description of the invariant.
        name: Name of the invariant (may provide hints).

    Returns:
        Python expression string, or None if untranslatable.
    """
    text = logic.strip().rstrip('.').lower()
    rules = {
        r"output is (?:not empty|non-empty)": "len(output_data) > 0",
        r"(?:output is |output |result is |result )?(?:not none|is not none)|returns something": "output_data is not None",
        r"(?:output|result)(?: value)? is (?:always )?(?:positive|greater than zero|>\s*0)": "output_data > 0",
        r"(?:output|result)(?: value)? is (?:always )?(?:non-negative|nonnegative|>=\s*0)": "output_data >= 0",
        r"all (?:values in output|output values) are positive": "all(x > 0 for x in output_data)",
        r"all (?:values in output|output values) are non-negative": "all(x >= 0 for x in output_data)",
        r"(?:no duplicates in output|output (?:has no duplicates|is unique))": "len(output_data) == len(set(output_data))",
        r"no duplicate ids in output": "len(output_data['ids']) == len(set(output_data['ids']))",
        r"(?:sum|total) (?:is )?preserved": "sum(input_data['values']) == sum(output_data)",
        r"(?:length|count) preserved|same length|same size": "len(input_data['items'] if 'items' in input_data else input_data['values']) == len(output_data)",
        r"(?:output )?equals input|(?:output is )?same as input": "output_data == input_data",
    }
    for pattern, expression in rules.items():
        if re.fullmatch(pattern, text):
            return expression
    for kind, pytype in [('list', 'list'), ('dict', 'dict'), ('dictionary', 'dict'), ('string', 'str'), ('int', 'int'), ('integer', 'int'), ('bool', 'bool'), ('boolean', 'bool')]:
        if re.fullmatch(r"(?:output is|returns) (?:a|an) " + kind, text):
            return f"type(output_data) is {pytype}"
    match = re.fullmatch(r"output (?:is |equals )?(?:double|twice|doubled) (?:the )?(?:input )?([a-z_]\w*)", text)
    if match:
        return f"output_data == input_data[{match[1]!r}] * 2"
    match = re.fullmatch(r"output (?:is )?between (-?\d+(?:\.\d+)?) and (-?\d+(?:\.\d+)?)", text)
    if match:
        return f"{match[1]} <= output_data <= {match[2]}"
    # Match quoted literals against original text to preserve case and escaping.
    match = re.fullmatch(r"[Oo]utput (starts|ends) with (['\"])(.*?)\2", logic.strip().rstrip('.'))
    if match:
        return f"output_data.{ 'startswith' if match[1] == 'starts' else 'endswith'}({match[3]!r})"
    return None


def compute_script_hash(script: str) -> str:
    """Compute a hash for the verification script.

    Used to detect if a script has changed.

    Args:
        script: The script source code.

    Returns:
        SHA-256 hash of the script.
    """
    return hashlib.sha256(script.encode()).hexdigest()
