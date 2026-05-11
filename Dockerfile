FROM python:3.11-slim

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy server files
COPY mcp_server.py .
COPY tools/ tools/

# Expose port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the MCP HTTP server
CMD ["python", "mcp_server.py", "--host", "0.0.0.0", "--port", "8000"]
