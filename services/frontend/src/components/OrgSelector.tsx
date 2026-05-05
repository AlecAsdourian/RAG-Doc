import { Building2, Check } from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

interface Organization {
  id: string;
  name: string;
  slug: string;
}

interface OrgSelectorProps {
  organizations: Organization[];
  selectedOrgId: string;
  onSelect: (orgId: string) => void;
  isLoading?: boolean;
}

export function OrgSelector({
  organizations,
  selectedOrgId,
  onSelect,
  isLoading = false,
}: OrgSelectorProps) {
  const selectedOrg = organizations.find((org) => org.id === selectedOrgId);

  return (
    <div className="org-selector p-4 border-b border-[var(--color-border)]">
      <label className="text-xs font-semibold text-[var(--color-muted-foreground)] uppercase tracking-wide mb-2 block">
        Organization
      </label>

      <Select value={selectedOrgId} onValueChange={onSelect} disabled={isLoading}>
        <SelectTrigger className="w-full bg-[var(--color-card)] border-[var(--color-border)] hover:bg-[var(--color-secondary)] transition-colors">
          <div className="flex items-center gap-2 flex-1 overflow-hidden">
            <Building2 className="h-4 w-4 text-[var(--color-muted-foreground)] flex-shrink-0" />
            <SelectValue>
              {selectedOrg ? (
                <span className="truncate font-medium">{selectedOrg.name}</span>
              ) : (
                <span className="text-[var(--color-muted-foreground)]">Select organization</span>
              )}
            </SelectValue>
          </div>
        </SelectTrigger>
        <SelectContent>
          {organizations.map((org) => (
            <SelectItem key={org.id} value={org.id}>
              <div className="flex items-center justify-between w-full gap-3">
                <div className="flex items-center gap-2 overflow-hidden">
                  <Building2 className="h-4 w-4 text-[var(--color-muted-foreground)] flex-shrink-0" />
                  <span className="truncate">{org.name}</span>
                </div>
                {org.id === selectedOrgId && (
                  <Check className="h-4 w-4 text-[var(--color-accent)] flex-shrink-0" />
                )}
              </div>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
