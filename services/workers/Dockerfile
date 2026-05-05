FROM python:3.11-slim

WORKDIR /app

# Copy requirements file
COPY requirements.txt ./

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Run the workers module
CMD ["python", "-m", "workers"]
