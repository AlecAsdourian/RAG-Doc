import { useState, useEffect } from 'react';

export interface Repository {
  id: string;
  name: string;
  url: string;
  status: 'ready' | 'indexing' | 'error';
}

interface UseRepositoriesResult {
  repositories: Repository[];
  selectedRepo: Repository | null;
  setSelectedRepoId: (id: string) => void;
  isLoading: boolean;
  error: string | null;
}

// Hardcoded list for now - will be replaced with API call later
const MOCK_REPOSITORIES: Repository[] = [
  {
    id: 'demo-repo',
    name: 'Demo Repository',
    url: 'https://github.com/example/demo-repo',
    status: 'ready',
  },
  {
    id: 'frontend',
    name: 'Frontend App',
    url: 'https://github.com/example/frontend',
    status: 'ready',
  },
  {
    id: 'backend',
    name: 'Backend Service',
    url: 'https://github.com/example/backend',
    status: 'indexing',
  },
];

export function useRepositories(initialRepoId?: string): UseRepositoriesResult {
  const [repositories] = useState<Repository[]>(MOCK_REPOSITORIES);
  const [selectedRepoId, setSelectedRepoId] = useState<string>(
    initialRepoId || MOCK_REPOSITORIES[0]?.id || ''
  );
  const [isLoading] = useState(false);
  const [error] = useState<string | null>(null);

  // Find the selected repository
  const selectedRepo = repositories.find((r) => r.id === selectedRepoId) || null;

  // TODO: Replace with actual API call
  useEffect(() => {
    // Future: Fetch repositories from API
    // const fetchRepositories = async () => {
    //   setIsLoading(true);
    //   try {
    //     const response = await apiClient.getRepositories();
    //     setRepositories(response);
    //   } catch (e) {
    //     setError(e instanceof Error ? e.message : 'Failed to fetch repositories');
    //   } finally {
    //     setIsLoading(false);
    //   }
    // };
    // fetchRepositories();
  }, []);

  return {
    repositories,
    selectedRepo,
    setSelectedRepoId,
    isLoading,
    error,
  };
}
