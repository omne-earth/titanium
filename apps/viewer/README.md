# Titanium Viewer

Web UI for browsing and inspecting Titanium jobs, trials, and trajectories.

## Development

Start the frontend dev server with hot reloading:

```bash
bun install
bun dev
```

The frontend will be available at `http://localhost:5173`.

For full development with the backend API, use the Titanium CLI from the repository root:

```bash
titanium view ./jobs --dev
```

This starts both the backend API server and the frontend dev server with proper configuration.

## Building

Build the production bundle:

```bash
bun run build
```

Output is written to `build/client/` with static assets ready to be served.

### Deploying changes to `titanium view`

`titanium view` serves static files from `src/titanium/viewer/static/`, **not** directly from `apps/viewer/build/client/`. After editing frontend code, you need to both build and copy the output. The easiest way:

```bash
# Option 1: Let titanium do it (recommended)
titanium view ./jobs --build

# Option 2: Manual build + copy
cd apps/viewer
bun run build
rm -rf ../../src/titanium/viewer/static
cp -r build/client ../../src/titanium/viewer/static
```

After either option, restart the `titanium view` server for changes to take effect.

## Stack

- React 19 with React Router 7
- TanStack Query for data fetching
- TanStack Table for sortable tables
- Tailwind CSS v4 for styling
- shadcn/ui components
- Shiki for syntax highlighting
