import type { ChunkResult } from '@/lib/api/types';

export type FileType = 'all' | 'code' | 'docs';

export interface Organization {
  id: string;
  name: string;
  slug: string;
  repoCount: number;
}

export interface SearchResult {
  id: string;
  fileName: string;
  filePath: string;
  language: string;
  summary: string;
  repoName: string;
  content: string;
  type: 'code' | 'docs';
  relevanceScore: number;
  matchedLines: number[];
  chunkIndex: number;
  totalChunks: number;
}

export interface IndexedRepo {
  id: string;
  name: string;
  status: 'ready' | 'indexing' | 'error';
}

const EXT_TO_LANG: Record<string, string> = {
  ts: 'typescript',
  tsx: 'typescript',
  js: 'javascript',
  jsx: 'javascript',
  py: 'python',
  go: 'go',
  rs: 'rust',
  java: 'java',
  rb: 'ruby',
  css: 'css',
  html: 'html',
  json: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  md: 'markdown',
  sql: 'sql',
  sh: 'bash',
  bash: 'bash',
  c: 'c',
  cpp: 'cpp',
  h: 'c',
  hpp: 'cpp',
};

const DOC_EXTENSIONS = new Set(['md', 'txt', 'rst', 'adoc', 'html']);

export function mapChunkToSearchResult(
  chunk: ChunkResult,
  index: number,
  repoName = 'unknown/repo'
): SearchResult {
  const parts = chunk.file_path.split('/');
  const fileName = parts[parts.length - 1] || chunk.file_path;
  const ext = fileName.split('.').pop()?.toLowerCase() || '';
  const language = EXT_TO_LANG[ext] || ext;
  const type = DOC_EXTENSIONS.has(ext) ? 'docs' : 'code';

  const matchedLines: number[] = [];
  if (chunk.start_line && chunk.end_line) {
    for (let i = chunk.start_line; i <= chunk.end_line; i++) {
      matchedLines.push(i);
    }
  }

  return {
    id: chunk.chunk_id || `result-${index}`,
    fileName,
    filePath: chunk.file_path,
    language,
    summary: chunk.content_preview || chunk.breadcrumb || '',
    repoName,
    content: chunk.content,
    type,
    relevanceScore: chunk.score,
    matchedLines,
    chunkIndex: index,
    totalChunks: 0,
  };
}
