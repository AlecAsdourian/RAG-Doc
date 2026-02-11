import { useEffect, useRef } from 'react';
import { Search, X, Loader2, Command } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  isLoading?: boolean;
  placeholder?: string;
  className?: string;
}

export function SearchInput({
  value,
  onChange,
  isLoading = false,
  placeholder = 'Search code, functions, or documentation...',
  className,
}: SearchInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  // Cmd/Ctrl + K to focus
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        inputRef.current?.focus();
      }
      // Escape to blur
      if (e.key === 'Escape' && document.activeElement === inputRef.current) {
        inputRef.current?.blur();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  const handleClear = () => {
    onChange('');
    inputRef.current?.focus();
  };

  return (
    <div className={cn('relative', className)}>
      {/* Search icon or loading spinner */}
      <div className="absolute left-3 top-1/2 -translate-y-1/2">
        {isLoading ? (
          <Loader2 className="h-4 w-4 text-[var(--color-muted-foreground)] animate-spin" />
        ) : (
          <Search className="h-4 w-4 text-[var(--color-muted-foreground)]" />
        )}
      </div>

      <Input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="h-10 w-full pl-10 pr-24 bg-[var(--color-secondary)] border-[var(--color-border)] placeholder:text-[var(--color-muted-foreground)] focus-visible:ring-[var(--color-ring)] transition-all duration-150"
      />

      {/* Right side: clear button and keyboard hint */}
      <div className="absolute right-3 top-1/2 -translate-y-1/2 flex items-center gap-2">
        {value && (
          <Button
            variant="ghost"
            size="icon"
            onClick={handleClear}
            className="h-6 w-6 text-[var(--color-muted-foreground)] hover:text-[var(--color-foreground)]"
            aria-label="Clear search"
          >
            <X className="h-3.5 w-3.5" />
          </Button>
        )}
        <kbd className="pointer-events-none hidden sm:flex h-5 select-none items-center gap-0.5 rounded border border-[var(--color-border)] bg-[var(--color-muted)] px-1.5 font-mono text-xs text-[var(--color-muted-foreground)]">
          <Command className="h-3 w-3" />K
        </kbd>
      </div>
    </div>
  );
}
