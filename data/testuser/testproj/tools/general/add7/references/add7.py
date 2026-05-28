import os

@register_tool(
    name="add7",
    description="Add two numbers",
    input_schema={"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}, "required": ["a", "b"]},
)
def add7_tool(args):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    tool_dir = os.path.dirname(script_dir)
    output_dir = os.path.join(tool_dir, "outputs")
    os.makedirs(output_dir, exist_ok=True)
    a = float(open(os.path.join(tool_dir, "inputs", "a.txt")).read().strip())
    b = float(open(os.path.join(tool_dir, "inputs", "b.txt")).read().strip())
    result = a + b
    with open(os.path.join(output_dir, "c.txt"), "w", encoding="utf-8") as f:
        f.write(str(result))
    with open(os.path.join(output_dir, "results.txt"), "w", encoding="utf-8") as f:
        f.write(str(result))
    with open(os.path.join(output_dir, "status.txt"), "w", encoding="utf-8") as f:
        f.write("success")
    return {"content": [{"type": "text", "text": f"Done: {result}"}]}
