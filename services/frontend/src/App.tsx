import { useEffect, useState } from 'react';
import { Layout } from '@/components/Layout';
import { SearchInput } from '@/components/SearchInput';
import { SearchResults } from '@/components/SearchResults';
import { ChatPanel } from '@/components/ChatPanel';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { useSearch } from '@/hooks/useSearch';
import { initTheme } from '@/lib/theme';
import type { SourceInfo } from '@/lib/api/types';

// TODO: Replace with actual repository selector
const DEFAULT_REPOSITORY_ID = 'demo-repo';

function App() {
  const [activeTab, setActiveTab] = useState<string>('search');

  useEffect(() => {
    initTheme();
  }, []);

  const { query, setQuery, results, totalResults, isLoading, error } = useSearch({
    repositoryId: DEFAULT_REPOSITORY_ID,
  });

  // When a chat source is clicked, switch to search and show it
  const handleSourceClick = (source: SourceInfo) => {
    // For now, just switch to search tab and search for the file
    setActiveTab('search');
    setQuery(source.file_path);
  };

  return (
    <Layout>
      <div className="mx-auto max-w-4xl px-4 py-6 sm:px-6 lg:px-8">
        <Tabs value={activeTab} onValueChange={setActiveTab} className="w-full">
          <TabsList className="mb-6">
            <TabsTrigger value="search">Search</TabsTrigger>
            <TabsTrigger value="chat">Chat</TabsTrigger>
          </TabsList>

          <TabsContent value="search" className="mt-0">
            {/* Search input */}
            <div className="mb-8">
              <SearchInput
                value={query}
                onChange={setQuery}
                isLoading={isLoading}
              />
            </div>

            {/* Results */}
            <SearchResults
              query={query}
              results={results}
              totalResults={totalResults}
              isLoading={isLoading}
              error={error}
            />
          </TabsContent>

          <TabsContent value="chat" className="mt-0 h-[calc(100vh-200px)]">
            <ChatPanel
              repositoryId={DEFAULT_REPOSITORY_ID}
              onSourceClick={handleSourceClick}
            />
          </TabsContent>
        </Tabs>
      </div>
    </Layout>
  );
}

export default App;
