"use client";

import { useState } from "react";
import { SettingRow, SettingsSection } from "@/components/product/setting-row";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { endSession } from "@/lib/session-end";

/** One person's sign-ins end together: every device, this one included. */
export function SignOutEverywhereSection() {
  const [open, setOpen] = useState(false);
  return (
    <SettingsSection id="signed-in" title="Where you’re signed in">
      <SettingRow
        label="Sign out everywhere"
        description="Ends your sign-in on every phone and computer, including this one."
        wrapDescription
        control={
          <Button variant="outline" onClick={() => setOpen(true)}>
            Sign out everywhere
          </Button>
        }
      />
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Sign out everywhere?"
        description="You will be signed out on every phone and computer, including this one. Sign in again to keep going."
        confirmLabel="Sign out everywhere"
        onConfirm={() => endSession("sign-out-everywhere")}
      />
    </SettingsSection>
  );
}
