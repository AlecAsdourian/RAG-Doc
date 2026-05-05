# Load environment variables from .env file
Get-Content .env | ForEach-Object {
    if ($_ -match '^([^#].+?)=(.+)$') {
        $key = $matches[1].Trim()
        $value = $matches[2].Trim()
        [Environment]::SetEnvironmentVariable($key, $value, 'Process')
        Write-Host "Loaded: $key"
    }
}

Write-Host "`nStarting backend server...`n"

# Start the Go server
go run main.go
