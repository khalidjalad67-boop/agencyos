import ast
import re
import importlib
import sys
from dataclasses import dataclass, asdict
from typing import Dict, Any, List, Optional, Set, Tuple

@dataclass
class TesterResult:
    opportunity_id: str
    passed: bool
    checked_symbols: List[str]
    unresolved_symbols: List[str]
    feedback: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

class Tester:
    """Deterministic, model-free verification step running after Worker and before Reviewer.
    Inspects Python code snippets in Worker output and checks whether referenced module
    attributes, methods, and symbols actually exist in the live Python environment via hasattr().
    """

    KNOWN_TYPE_CONVENTIONS = {
        "curses": {
            "aliases": {"stdscr", "window", "win", "pad", "screen"},
            "type_resolver": lambda: getattr(importlib.import_module("curses"), "window", None)
        }
    }

    def __init__(self):
        pass

    def check(self, task_spec: Any, worker_result: Any) -> TesterResult:
        """Evaluates worker_result output for checkable Python symbol existence."""
        opp_id = (
            getattr(task_spec, "opportunity_id", None) or
            getattr(worker_result, "opportunity_id", "") if worker_result else ""
        )
        output = getattr(worker_result, "output", "") if worker_result else ""

        if not output:
            return TesterResult(
                opportunity_id=opp_id,
                passed=True,
                checked_symbols=[],
                unresolved_symbols=[],
                feedback="Deterministic Tester check passed: empty worker output."
            )

        code_blocks = self._extract_code_blocks(output)
        checked_symbols: List[str] = []
        unresolved_symbols: List[str] = []

        for code in code_blocks:
            checked, unresolved = self._verify_code_snippet(code)
            for s in checked:
                if s not in checked_symbols:
                    checked_symbols.append(s)
            for s in unresolved:
                if s not in unresolved_symbols:
                    unresolved_symbols.append(s)

        if unresolved_symbols:
            formatted_unresolved = ", ".join(f"'{sym}'" for sym in unresolved_symbols)
            feedback = f"Deterministic Tester check failed: unresolved symbol(s) {formatted_unresolved} not found in live Python environment."
            return TesterResult(
                opportunity_id=opp_id,
                passed=False,
                checked_symbols=checked_symbols,
                unresolved_symbols=unresolved_symbols,
                feedback=feedback
            )

        if checked_symbols:
            feedback = f"Deterministic Tester check passed: verified {len(checked_symbols)} symbols ({', '.join(checked_symbols[:5])})."
        else:
            feedback = "Deterministic Tester check passed: no checkable symbols found (inconclusive)."

        return TesterResult(
            opportunity_id=opp_id,
            passed=True,
            checked_symbols=checked_symbols,
            unresolved_symbols=[],
            feedback=feedback
        )

    def _extract_code_blocks(self, text: str) -> List[str]:
        """Extracts python code blocks from text, falling back to full text if no code fences exist."""
        # Find ```python ... ``` or ```py ... ``` or generic ``` ... ```
        pattern = r"```(?:python|py)?\s*\n(.*?)```"
        blocks = re.findall(pattern, text, flags=re.DOTALL | re.IGNORECASE)
        if blocks:
            return [b.strip() for b in blocks if b.strip()]
        return [text]

    def _verify_code_snippet(self, code: str) -> Tuple[List[str], List[str]]:
        """Verifies imports and attribute accesses in a single code snippet."""
        checked: List[str] = []
        unresolved: List[str] = []

        try:
            tree = ast.parse(code)
        except SyntaxError:
            # If full snippet has syntax error (e.g. diff markers), scan line by line
            return self._scan_with_regex(code)

        # 1. Map imports in the snippet
        imported_modules: Dict[str, str] = {}  # alias -> full module name
        imported_objects: Dict[str, Tuple[str, str]] = {}  # alias -> (module, object_name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    imported_modules[local_name] = alias.name
            elif isinstance(node, ast.ImportFrom):
                mod_name = node.module or ""
                for alias in node.names:
                    local_name = alias.asname or alias.name
                    imported_objects[local_name] = (mod_name, alias.name)
                    # Check if imported object actually exists in the module
                    try:
                        mod = importlib.import_module(mod_name)
                        checked.append(f"{mod_name}.{alias.name}")
                        if not hasattr(mod, alias.name):
                            unresolved.append(f"{mod_name}.{alias.name}")
                    except Exception:
                        pass

        # 2. Check attribute accesses on imported modules, objects, and known conventions
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                attr_name = node.attr
                val = node.value

                # Case A: direct access on imported module (e.g. curses.getattrs or os.path)
                if isinstance(val, ast.Name):
                    var_name = val.id
                    if var_name in imported_modules:
                        mod_name = imported_modules[var_name]
                        try:
                            mod = importlib.import_module(mod_name)
                            sym_name = f"{mod_name}.{attr_name}"
                            checked.append(sym_name)
                            if not hasattr(mod, attr_name):
                                unresolved.append(sym_name)
                        except Exception:
                            pass
                    elif var_name in imported_objects:
                        mod_name, obj_name = imported_objects[var_name]
                        try:
                            mod = importlib.import_module(mod_name)
                            target_obj = getattr(mod, obj_name, None)
                            if target_obj is not None:
                                sym_name = f"{mod_name}.{obj_name}.{attr_name}"
                                checked.append(sym_name)
                                if not hasattr(target_obj, attr_name):
                                    unresolved.append(sym_name)
                        except Exception:
                            pass
                    else:
                        # Check known type conventions if module was imported
                        for mod_key, conv in self.KNOWN_TYPE_CONVENTIONS.items():
                            if mod_key in imported_modules.values() and var_name in conv["aliases"]:
                                try:
                                    target_cls = conv["type_resolver"]()
                                    if target_cls is not None:
                                        sym_name = f"{mod_key}.window.{attr_name}"
                                        checked.append(sym_name)
                                        mod_obj = importlib.import_module(mod_key)
                                        if not hasattr(target_cls, attr_name) and not hasattr(mod_obj, attr_name):
                                            unresolved.append(sym_name)
                                except Exception:
                                    pass

                # Case B: chained access (e.g. curses.window.getattrs or os.path.exists)
                elif isinstance(val, ast.Attribute) and isinstance(val.value, ast.Name):
                    base_mod = val.value.id
                    sub_name = val.attr
                    if base_mod in imported_modules:
                        full_mod_name = imported_modules[base_mod]
                        try:
                            mod = importlib.import_module(full_mod_name)
                            if hasattr(mod, sub_name):
                                sub_obj = getattr(mod, sub_name)
                                sym_name = f"{full_mod_name}.{sub_name}.{attr_name}"
                                checked.append(sym_name)
                                if not hasattr(sub_obj, attr_name):
                                    unresolved.append(sym_name)
                        except Exception:
                            pass

        return checked, unresolved

    def _scan_with_regex(self, text: str) -> Tuple[List[str], List[str]]:
        """Fallback regex scanner for snippets where AST parsing fails."""
        checked: List[str] = []
        unresolved: List[str] = []

        # Check for curses stdscr.<attr> or curses.<attr>
        if "curses" in text:
            try:
                curses_mod = importlib.import_module("curses")
                window_cls = getattr(curses_mod, "window", None)
                matches = re.findall(r"\b(?:stdscr|window|win)\.([a-zA-Z_][a-zA-Z0-9_]*)\b", text)
                for attr in matches:
                    sym = f"curses.window.{attr}"
                    checked.append(sym)
                    if window_cls and not hasattr(window_cls, attr) and not hasattr(curses_mod, attr):
                        unresolved.append(sym)
            except Exception:
                pass

        return checked, unresolved
