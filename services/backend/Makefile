.PHONY: lint fmt test migrate-create migrate-up migrate-down migrate-version seed seed-lineage seed-complete

# Code quality
lint:
	golangci-lint run

fmt:
	gofmt -w .

test:
	go test ./... -v

# Database migrations
DATABASE_URL ?= postgres://smartdocs:dev_password_change_in_prod@localhost:5432/smartdocs?sslmode=disable

migrate-create:
	@if [ -z "$(NAME)" ]; then echo "Usage: make migrate-create NAME=migration_name"; exit 1; fi
	migrate create -ext sql -dir migrations -seq $(NAME)

migrate-up:
	migrate -path migrations -database "$(DATABASE_URL)" up

migrate-down:
	migrate -path migrations -database "$(DATABASE_URL)" down 1

migrate-version:
	migrate -path migrations -database "$(DATABASE_URL)" version

seed:
	psql "$(DATABASE_URL)" -f scripts/seed.sql

seed-lineage:
	psql "$(DATABASE_URL)" -f scripts/seed-lineage.sql

seed-complete:
	psql "$(DATABASE_URL)" -f scripts/seed-complete.sql
