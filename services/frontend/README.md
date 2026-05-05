# Frontend Service

Modern web interface for the Smart Documentation Platform.

## Tech Stack

- **React 19** - UI library
- **TypeScript** - Type safety
- **Vite** - Fast build tooling and dev server
- **ESLint** - Code linting

## Development

### Install Dependencies

```bash
npm install
```

### Run Development Server

```bash
npm run dev
```

The dev server will start at `http://localhost:5173` with hot module replacement (HMR).

### Build for Production

```bash
npm run build
```

Build output will be in the `dist/` directory.

### Code Quality

```bash
# Lint code
npm run lint

# Format code
npm run fmt

# Run tests
npm run test
```

### Preview Production Build

```bash
npm run preview
```

## Tooling

- **ESLint**: TypeScript and React linting with modern flat config
- **Prettier**: Code formatting (single quotes, 2 spaces, trailing commas)
- **Vitest**: Fast unit testing with React Testing Library
- **TypeScript**: Static type checking

## Project Structure

```
src/
├── components/     # React components
├── lib/           # Utility functions and helpers
├── assets/        # Static assets
├── App.tsx        # Main application component
└── main.tsx       # Application entry point
```

## Status

Initial setup complete. UI features and styling to be added in later phases.
