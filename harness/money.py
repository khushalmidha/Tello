"""Small Decimal expression language. Model output is data, never executable Python."""
from __future__ import annotations

import ast
from datetime import date
from decimal import Decimal, ROUND_CEILING, ROUND_HALF_UP, localcontext

CENT = Decimal("0.01")
FUNCTIONS = {"cents", "ceil", "min", "max", "days"}


def decimal(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("money must be finite")
    return result


def cents(value) -> Decimal:
    return decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def usd(value) -> str:
    value = cents(value)
    if value < 0:
        raise ValueError("a finding cannot have a negative amount")
    return format(value, ".2f")


def evaluate(expression: str, variables: dict | None = None) -> Decimal:
    if not isinstance(expression, str) or len(expression) > 4096:
        raise ValueError("invalid calculation expression")
    tree = ast.parse(expression, mode="eval")
    pending, nodes = [(tree, 0)], 0
    while pending:
        node, depth = pending.pop()
        nodes += 1
        if nodes > 2048 or depth > 700:
            raise ValueError("calculation is too complex")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    variables = variables or {}

    def visit(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float, str)):
                raise ValueError("invalid constant")
            # Use the original numeric token: converting a binary float loses exact decimals.
            token = node.value if isinstance(node.value, str) else ast.get_source_segment(expression, node)
            return decimal(token)
        if isinstance(node, ast.Name) and node.id in variables:
            return decimal(variables[node.id])
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            value = visit(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        if isinstance(node, ast.BinOp):
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            if node.func.id == "days" and len(node.args) == 2:
                values = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if len(values) == 2:
                    return Decimal((date.fromisoformat(values[0]) - date.fromisoformat(values[1])).days)
                raise ValueError("days requires two ISO dates")
            args = [visit(a) for a in node.args]
            if node.func.id == "cents" and len(args) == 1:
                return cents(args[0])
            if node.func.id == "ceil" and len(args) == 1:
                return args[0].to_integral_value(rounding=ROUND_CEILING)
            if node.func.id in ("min", "max") and args:
                return (min if node.func.id == "min" else max)(args)
        raise ValueError(f"unsupported calculation node: {type(node).__name__}")

    with localcontext() as context:
        context.prec = 40
        result = visit(tree.body)
    if not result.is_finite() or abs(result) > Decimal("1e15"):
        raise ValueError("calculation out of range")
    return result


def calculation_values(plan: dict) -> dict:
    """Source validation happens at the proposal boundary; arithmetic stays deterministic."""
    values = {}
    for source in plan.get("inputs", []):
        name = source["name"]
        if not isinstance(name, str) or not name.isidentifier() or name in values or name in FUNCTIONS:
            raise ValueError("calculation inputs must have unique identifier names")
        values[name] = decimal(source["value"])
    for step in plan.get("steps", []):
        name = step["name"]
        if not isinstance(name, str) or not name.isidentifier() or name in values or name in FUNCTIONS:
            raise ValueError("calculation step names must be unique identifiers")
        values[name] = evaluate(step["expression"], values)
    return values


def calculate(plan: dict) -> tuple[str, dict]:
    """Round lines only where explicitly requested; convert once after summing a finding."""
    values = calculation_values(plan)
    amount = evaluate(plan["amount"], values)
    currency = plan.get("currency", "USD").upper()
    if currency == "INR":
        fx = evaluate(plan["fx"], values)
        if fx <= 0:
            raise ValueError("FX must be positive")
        amount /= fx
    elif currency != "USD":
        raise ValueError(f"unsupported contract currency: {currency}")
    return usd(amount), {key: str(value) for key, value in values.items()}
