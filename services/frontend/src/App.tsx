import { useEffect } from 'react';
import { Layout } from '@/components/Layout';
import { initTheme } from '@/lib/theme';

function App() {
  useEffect(() => {
    initTheme();
  }, []);

  return (
    <Layout>
      <div className="p-6">
        <p className="text-[var(--color-muted-foreground)]">
          Search and chat coming soon...
        </p>
      </div>
    </Layout>
  );
}

export default App;
