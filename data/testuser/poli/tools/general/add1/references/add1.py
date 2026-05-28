import os
from datetime import datetime

@register_tool(
    name="add1",
    description="Add two numbers",
    input_schema={"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]},
)
def add1_tool(args):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tool_dir = os.path.dirname(script_dir)
    output_dir = os.path.join(tool_dir, "outputs")
    os.makedirs(output_dir, exist_ok=True)
    a = float(open(os.path.join(tool_dir, "inputs", "a.txt")).read().strip())
    b = float(open(os.path.join(tool_dir, "inputs", "b.txt")).read().strip())
    result = a + b
    for name in ("c", "results", "status"):
        val = str(result) if name != "status" else "success"
        with open(os.path.join(output_dir, f"{name}.txt"), "w", encoding="utf-8") as f:
            f.write(val)
    return {"content": [{"type": "text", "text": f"Done: {result}"}]}
