import { useEffect, useState } from 'react';
import { Layout } from '@/components/Layout';
import { MainLayout, MainLayoutMobile } from '@/components/MainLayout';
import { LandingPage } from '@/components/LandingPage';
import { useAuth, isAuthEnabled } from '@/hooks/useAuth';
import { initTheme } from '@/lib/theme';
import type { SourceInfo } from '@/lib/api/types';

const DEFAULT_REPOSITORY_ID = 'demo-repo';

function App() {
  const [selectedRepo, setSelectedRepo] = useState(DEFAULT_REPOSITORY_ID);
  const { user, loading, signInWithGitHub, signInWithGitLab, signOut } = useAuth();

  useEffect(() => {
    initTheme();
  }, []);

  // When a chat source is clicked, we could highlight it in search
  const handleSourceClick = (source: SourceInfo) => {
    // For now, just log it - could navigate to file or highlight
    console.log('Source clicked:', source.file_path);
  };

  // Handle repository selection change
  const handleRepoChange = (repoId: string) => {
    setSelectedRepo(repoId);
  };

  // Show loading state while checking auth
  if (loading) {
    return (
      <Layout showAuth={false}>
        <div className="flex items-center justify-center h-[calc(100vh-65px)]">
          <div className="animate-pulse text-[var(--color-muted-foreground)]">
            Loading...
          </div>
        </div>
      </Layout>
    );
  }

  // If auth is enabled and user is not logged in, show landing page
  if (isAuthEnabled() && !user) {
    return (
      <LandingPage
        onSignInWithGitHub={signInWithGitHub}
        onSignInWithGitLab={signInWithGitLab}
      />
    );
  }

  // Authenticated user or dev mode without auth - show main app
  return (
    <Layout
      user={user}
      onSignIn={signInWithGitHub}
      onSignOut={signOut}
      showAuth={isAuthEnabled()}
    >
      {/* Desktop: side-by-side resizable panels */}
      <MainLayout
        repositoryId={selectedRepo}
        onSourceClick={handleSourceClick}
        onRepoChange={handleRepoChange}
      />
      {/* Mobile: stacked vertically */}
      <MainLayoutMobile
        repositoryId={selectedRepo}
        onSourceClick={handleSourceClick}
        onRepoChange={handleRepoChange}
      />
    </Layout>
  );
}

export default App;
