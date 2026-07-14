"""Count functions missing docstrings."""
import ast
import os

total = 0
missing = 0
for root, dirs, files in os.walk('src/edgelite'):
    for f in files:
        if not f.endswith('.py'):
            continue
        path = os.path.join(root, f)
        try:
            tree = ast.parse(open(path, encoding='utf-8', errors='ignore').read(), filename=path)
        except Exception:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                total += 1
                has_doc = (
                    node.body
                    and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)
                )
                if not has_doc:
                    missing += 1

print(f"Functions: {total}, Missing docstrings: {missing} ({missing*100//total}%)")
