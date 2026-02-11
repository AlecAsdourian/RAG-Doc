import { Github } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface LandingPageProps {
  onSignInWithGitHub: () => void;
  onSignInWithGitLab: () => void;
}

export function LandingPage({ onSignInWithGitHub, onSignInWithGitLab }: LandingPageProps) {
  return (
    <div className="min-h-screen flex flex-col">
      {/* Hero Section */}
      <main className="flex-1 flex flex-col items-center justify-center px-4 py-16">
        <div className="max-w-2xl mx-auto text-center">
          {/* Title */}
          <h1 className="text-4xl sm:text-5xl font-bold tracking-tight text-[var(--color-foreground)] mb-4">
            Search and understand your codebase
          </h1>

          {/* Subtitle */}
          <p className="text-lg text-[var(--color-muted-foreground)] mb-8 max-w-lg mx-auto">
            AI-powered semantic search and chat for your repositories.
            Find what you need, get answers with source citations.
          </p>

          {/* Sign in buttons */}
          <div className="flex flex-col sm:flex-row gap-3 justify-center">
            <Button
              onClick={onSignInWithGitHub}
              size="lg"
              className="gap-2 px-6"
            >
              <Github className="h-5 w-5" />
              Sign in with GitHub
            </Button>
            <Button
              onClick={onSignInWithGitLab}
              variant="outline"
              size="lg"
              className="gap-2 px-6"
            >
              <GitLabIcon className="h-5 w-5" />
              Sign in with GitLab
            </Button>
          </div>
        </div>

        {/* Features */}
        <div className="mt-20 max-w-4xl mx-auto grid grid-cols-1 md:grid-cols-3 gap-8 px-4">
          <FeatureCard
            title="Semantic Search"
            description="Find code by meaning, not just keywords. Understand function signatures, variable patterns, and documentation."
          />
          <FeatureCard
            title="AI Chat"
            description="Ask questions about your codebase and get accurate answers with source citations you can verify."
          />
          <FeatureCard
            title="Multi-Repo Support"
            description="Connect multiple repositories and search across your entire codebase from one place."
          />
        </div>
      </main>

      {/* Footer */}
      <footer className="border-t border-[var(--color-border)] py-6 px-4 text-center text-sm text-[var(--color-muted-foreground)]">
        Built with AI-powered code intelligence
      </footer>
    </div>
  );
}

function FeatureCard({ title, description }: { title: string; description: string }) {
  return (
    <div className="p-6 rounded-lg border border-[var(--color-border)] bg-[var(--color-card)] transition-colors duration-200 hover:border-[var(--color-ring)]/50">
      <h3 className="text-lg font-semibold text-[var(--color-foreground)] mb-2">
        {title}
      </h3>
      <p className="text-sm text-[var(--color-muted-foreground)]">
        {description}
      </p>
    </div>
  );
}

// Simple GitLab icon (since lucide-react doesn't have GitLab)
function GitLabIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="currentColor"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path d="M22.65 14.39L12 22.13 1.35 14.39a.84.84 0 0 1-.3-.94l1.22-3.78 2.44-7.51A.42.42 0 0 1 4.82 2a.43.43 0 0 1 .58 0 .42.42 0 0 1 .11.18l2.44 7.49h8.1l2.44-7.51A.42.42 0 0 1 18.6 2a.43.43 0 0 1 .58 0 .42.42 0 0 1 .11.18l2.44 7.51L23 13.45a.84.84 0 0 1-.35.94z" />
    </svg>
  );
}
