import { Panel, PanelGroup, PanelResizeHandle } from 'react-resizable-panels';
import { SearchPanel } from '@/components/SearchPanel';
import { ChatPanel } from '@/components/ChatPanel';
import type { SourceInfo } from '@/lib/api/types';

interface MainLayoutProps {
  repositoryId: string;
  onSourceClick?: (source: SourceInfo) => void;
}

export function MainLayout({ repositoryId, onSourceClick }: MainLayoutProps) {
  return (
    <PanelGroup
      direction="horizontal"
      className="h-[calc(100vh-65px)] md:flex hidden"
    >
      <Panel defaultSize={50} minSize={30}>
        <SearchPanel repositoryId={repositoryId} />
      </Panel>
      <PanelResizeHandle className="w-1 bg-[var(--color-border)] hover:bg-[var(--color-primary)]/20 transition-colors duration-200" />
      <Panel defaultSize={50} minSize={30}>
        <ChatPanel repositoryId={repositoryId} onSourceClick={onSourceClick} />
      </Panel>
    </PanelGroup>
  );
}

// Mobile stacked layout for screens below 768px
export function MainLayoutMobile({ repositoryId, onSourceClick }: MainLayoutProps) {
  return (
    <div className="flex flex-col h-[calc(100vh-65px)] md:hidden">
      <div className="flex-1 border-b border-[var(--color-border)] overflow-hidden">
        <SearchPanel repositoryId={repositoryId} />
      </div>
      <div className="flex-1 overflow-hidden">
        <ChatPanel repositoryId={repositoryId} onSourceClick={onSourceClick} />
      </div>
    </div>
  );
}
