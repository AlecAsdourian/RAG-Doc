import { useEffect, useState } from 'react';
import { Layout } from '@/components/Layout';
import { MainLayout, MainLayoutMobile } from '@/components/MainLayout';
import { initTheme } from '@/lib/theme';
import type { SourceInfo } from '@/lib/api/types';

// TODO: Replace with actual repository selector
const DEFAULT_REPOSITORY_ID = 'demo-repo';

function App() {
  const [selectedRepo] = useState(DEFAULT_REPOSITORY_ID);

  useEffect(() => {
    initTheme();
  }, []);

  // When a chat source is clicked, we could highlight it in search
  const handleSourceClick = (source: SourceInfo) => {
    // For now, just log it - could navigate to file or highlight
    console.log('Source clicked:', source.file_path);
  };

  return (
    <Layout>
      {/* Desktop: side-by-side resizable panels */}
      <MainLayout
        repositoryId={selectedRepo}
        onSourceClick={handleSourceClick}
      />
      {/* Mobile: stacked vertically */}
      <MainLayoutMobile
        repositoryId={selectedRepo}
        onSourceClick={handleSourceClick}
      />
    </Layout>
  );
}

export default App;
