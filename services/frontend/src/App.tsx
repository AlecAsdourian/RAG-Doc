import { Routes, Route, Navigate, Outlet } from 'react-router-dom';
import { Loader2 } from 'lucide-react';
import { useAuth, isAuthEnabled } from '@/hooks/useAuth';
import { AppShell } from '@/components/AppShell';
import { LoginPage } from '@/pages/LoginPage';
import { OrgSelectPage } from '@/pages/OrgSelectPage';
import { SearchPage } from '@/pages/SearchPage';
import { ChatPage } from '@/pages/ChatPage';
import { RepoSettingsPage } from '@/pages/RepoSettingsPage';
import { KnowledgeGraphPage } from '@/pages/KnowledgeGraphPage';
import { LivingDocsPage } from '@/pages/LivingDocsPage';

function AuthGuard() {
  const { user } = useAuth();
  if (isAuthEnabled() && !user) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}

function App() {
  const { loading } = useAuth();

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-background">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <Routes>
      {/* Public */}
      <Route path="/login" element={<LoginPage />} />

      {/* Auth-guarded pages inside AppShell */}
      <Route element={<AuthGuard />}>
        <Route element={<AppShell />}>
          <Route path="/org"      element={<OrgSelectPage />} />
          <Route path="/search"   element={<SearchPage />} />
          <Route path="/chat"     element={<ChatPage />} />
          <Route path="/settings" element={<RepoSettingsPage />} />
          <Route path="/graph"    element={<KnowledgeGraphPage />} />
          <Route path="/docs"     element={<LivingDocsPage />} />
          <Route index            element={<Navigate to="/search" replace />} />
        </Route>
      </Route>

      {/* Catch-all */}
      <Route path="*" element={<Navigate to="/login" replace />} />
    </Routes>
  );
}

export default App;
