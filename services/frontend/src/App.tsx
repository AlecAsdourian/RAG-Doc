import { useEffect } from 'react';
import { Layout } from '@/components/Layout';
import { SearchInput } from '@/components/SearchInput';
import { SearchResults } from '@/components/SearchResults';
import { useSearch } from '@/hooks/useSearch';
import { initTheme } from '@/lib/theme';

// TODO: Replace with actual repository selector
const DEFAULT_REPOSITORY_ID = 'demo-repo';

function App() {
  useEffect(() => {
    initTheme();
  }, []);

  const { query, setQuery, results, totalResults, isLoading, error } = useSearch({
    repositoryId: DEFAULT_REPOSITORY_ID,
  });

  return (
    <Layout>
      <div className="mx-auto max-w-4xl px-4 py-6 sm:px-6 lg:px-8">
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
      </div>
    </Layout>
  );
}

export default App;
