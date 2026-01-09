FROM golang:1.21-alpine

WORKDIR /app

# Install golang-migrate
RUN go install -tags 'postgres' github.com/golang-migrate/migrate/v4/cmd/migrate@latest

# Copy go module files
COPY go.mod ./

# Download dependencies
RUN go mod download

# Copy source code
COPY . .

# Expose port
EXPOSE 8080

# Run the application
CMD ["go", "run", "main.go"]
