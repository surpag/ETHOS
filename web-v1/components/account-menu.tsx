"use client";

import { UserButton } from "@clerk/nextjs";
import { Settings } from "lucide-react";

import { ThemeToggle } from "./theme-toggle";

type AccountMenuProps = {
  onOpenSettings: () => void;
};

// Clerk owns account actions. ETHOS only adds its own settings entry.
export function AccountMenu({ onOpenSettings }: AccountMenuProps) {
  return (
    <div className="flex items-center justify-between rounded-xl px-2 py-1 hover:bg-surface-subtle">
      <div className="min-w-0 flex-1 truncate">
        <UserButton showName userProfileMode="modal" />
      </div>
      <div className="flex shrink-0 items-center gap-1">
        <ThemeToggle />
        <button aria-label="ETHOS 设置" className="ui-icon-button shrink-0" onClick={onOpenSettings} title="ETHOS 设置" type="button"><Settings className="size-4" /></button>
      </div>
    </div>
  );
}
