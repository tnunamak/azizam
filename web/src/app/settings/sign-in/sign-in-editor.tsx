"use client";
import { useLayoutEffect, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ApiError } from "@/lib/api";
import { toast } from "@/lib/toast";
import {
  dismissNotice,
  getSignIn,
  linkOwner,
  saveSignIn,
  signInKeys,
  type ProviderConnection,
  type SaveRequest,
  type SignInProposal,
  type SignInRules,
  type SignInSnapshot,
} from "@/lib/signin-api";
import { Button } from "@/components/ui/button";
import { ReadOnlyProvider } from "./bits";
import { ConnectionBlock } from "./connection-block";
import {
  APPLY_NEXT,
  blockerCaption,
  changeCount,
  CHECKING,
  DEFAULTS,
  formatWhen,
  leadingBlocker,
  mid,
  NOTHING_TO_SAVE,
  noFreshWarning,
  personChange,
  PREVIEW_FAILED,
  providerLabel,
  providerStatus as providerStatusCopy,
  READ_ONLY_NOTE,
  relinkNote,
  savedToast,
  sinceText,
  turnOnCaption,
  unlinkedNote,
  unlinkedNoteShort,
} from "./copy";
import {
  connectionSig,
  formFromSnapshot,
  formSig,
  hasSavedConnection,
  type Form,
} from "./form";
import { RulesEditor } from "./rules-editor";
import { SaveFooter } from "./save-footer";
import { SharedComputers } from "./shared-computers";
import { TestSignIn } from "./test-sign-in";
import { useDiscovery } from "./use-discovery";
import { useIsPhone } from "./use-media-query";
import { usePreview } from "./use-preview";
import { useSignInTest } from "./use-sign-in-test";
import { WaysToSignIn } from "./ways-to-sign-in";
import { WhatChanges } from "./what-changes";

/**
 * Everything below the section heading. It holds the form the owner is editing
 * and asks the server what saving would do; it never works out who gets in.
 */
export function SignInEditor({ snapshot }: { snapshot: SignInSnapshot }) {
  const client = useQueryClient();
  const phone = useIsPhone();
  const readOnly = !snapshot.viewer.can_edit;
  const live = snapshot.methods.provider;

  // The form the owner edits. A newer snapshot replaces it only while it is untouched.
  const [draft, setDraft] = useState(() => ({
    snapshot,
    form: formFromSnapshot(snapshot),
  }));
  if (draft.snapshot !== snapshot) {
    const untouched =
      formSig(draft.form) === formSig(formFromSnapshot(draft.snapshot));
    setDraft({
      snapshot,
      form: untouched ? formFromSnapshot(snapshot) : draft.form,
    });
  }
  const form = draft.form;
  const setForm = (update: (form: Form) => Form) =>
    setDraft((d) => ({ ...d, form: update(d.form) }));
  const baseForm = useMemo(() => formFromSnapshot(snapshot), [snapshot]);

  const [keep, setKeep] = useState<Record<string, boolean>>({});
  const [connectionOpen, setConnectionOpen] = useState(!live);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [linkedTestId, setLinkedTestId] = useState<string | null>(null);

  const label = providerLabel(form.provider.name, form.provider.address);
  const dirty = formSig(form) !== formSig(baseForm);
  const signature = connectionSig(form.provider);
  const unchanged = signature === connectionSig(baseForm.provider);
  const liveUnchanged = live && unchanged;
  // Saved, off, and the saved connection passed its last test: nothing new to prove.
  const savedTested = !live && unchanged && snapshot.provider.tested_at !== null;

  const discovery = useDiscovery(
    form.provider.address,
    snapshot.provider.address,
    !readOnly,
  );
  // A found provider fills the two plain defaults the owner left blank. This
  // runs before the browser can dispatch another event: a Test sign-in click
  // between the render and a later effect would test (and sign) the form
  // without them, and the passed test would not match the form on the page.
  useLayoutEffect(() => {
    if (!discovery.found || readOnly) return;
    setDraft((d) =>
      d.form.provider.scopes && d.form.provider.groups_claim
        ? d
        : {
            ...d,
            form: {
              ...d.form,
              provider: {
                ...d.form.provider,
                scopes: d.form.provider.scopes || DEFAULTS.scopes,
                groups_claim:
                  d.form.provider.groups_claim || DEFAULTS.groupsClaim,
              },
            },
          },
    );
  }, [discovery.found, readOnly]);

  const test = useSignInTest();
  const tested = test.passed?.signature === signature;
  const testId = tested ? (test.passed?.testId ?? null) : null;
  const testedAt = tested
    ? (test.passed?.at ?? null)
    : liveUnchanged || savedTested
      ? snapshot.provider.tested_at
      : null;
  // What the last passed test found about a fresh sign-in: this session's test, else the saved connection's. The saved
  // answer counts only once that connection has been tested (the snapshot says false for "not tested" too).
  const freshLogin: boolean | null = tested
    ? (test.passed?.freshLogin ?? null)
    : liveUnchanged || savedTested
      ? snapshot.provider.tested_at !== null
        ? snapshot.provider.fresh_login_supported
        : null
      : null;
  const freshWarning = freshLogin === false ? noFreshWarning(label) : null;

  // A turn-on is the intent whenever the provider is off, so the server can ask for the test.
  const proposal: SignInProposal = {
    ...form,
    methods: {
      password: form.methods.password,
      provider: live ? form.methods.provider : true,
    },
  };
  const { preview, settled, failed } = usePreview(proposal, testId, !readOnly);

  const patchConnection = (change: Partial<ProviderConnection>) =>
    setForm((f) => ({ ...f, provider: { ...f.provider, ...change } }));
  const setRules = (rules: SignInRules) => setForm((f) => ({ ...f, rules }));

  const replaceSnapshot = (next: SignInSnapshot) =>
    client.setQueryData(signInKeys.snapshot, next);

  const link = useMutation({
    mutationFn: (id: string) => linkOwner(id),
    onSuccess: (next, id) => {
      replaceSnapshot(next);
      setLinkedTestId(id);
      void client.invalidateQueries({ queryKey: signInKeys.snapshot });
      toast.success(`Linked. You can sign in with ${mid(label)} as the owner.`);
    },
    onError: (error) =>
      toast.error(error instanceof Error ? error.message : "Could not link."),
  });
  const dismiss = useMutation({
    mutationFn: () => dismissNotice(),
    onSuccess: replaceSnapshot,
  });

  const keepIds = useMemo(
    () =>
      (preview?.people ?? [])
        .filter((p) => (personChange(p, label)?.keys ?? 0) > 0 && keep[p.id])
        .map((p) => p.id),
    [preview, keep, label],
  );
  const save = useMutation({
    mutationFn: (forLater: boolean) => {
      const request: SaveRequest = {
        proposal: forLater
          ? { ...proposal, methods: { ...proposal.methods, provider: false } }
          : proposal,
        test_id: forLater ? null : testId,
        keep_admin_keys: forLater ? [] : keepIds,
        save_for_later: forLater,
      };
      return saveSignIn(snapshot.revision, request);
    },
    onSuccess: (result, forLater) => {
      setDraft({
        snapshot: result.snapshot,
        form: formFromSnapshot(result.snapshot),
      });
      replaceSnapshot(result.snapshot);
      setKeep({});
      void client.invalidateQueries({
        queryKey: [...signInKeys.snapshot, "preview"],
      });
      toast.success(
        savedToast({
          p: label,
          turnedOn: result.applied.provider_turned_on,
          forLater,
          removed: result.applied.admin_removed,
        }),
      );
    },
  });

  const reload = async () => {
    save.reset();
    const fresh = await client.fetchQuery({
      queryKey: signInKeys.snapshot,
      queryFn: getSignIn,
      staleTime: 0,
    });
    setDraft({ snapshot: fresh, form: formFromSnapshot(fresh) });
    setKeep({});
  };

  const discard = () => {
    setDraft({ snapshot, form: baseForm });
    setKeep({});
    save.reset();
    toast.success("Changes discarded.");
  };

  // ------------------------------------------------------------- the footer
  const blocker = preview ? leadingBlocker(preview.blockers, live) : undefined;
  const verdict = preview?.test_verdict ?? null;
  const unlinked = Boolean(testId) && verdict?.kind === "owner_unlinked";
  const turnOnIntent = !live && !form.methods.provider;
  let caption: string;
  let mobileCaption: string;
  let need = false;
  if (failed) {
    caption = mobileCaption = PREVIEW_FAILED;
    need = true;
  } else if (!preview) {
    caption = mobileCaption = CHECKING;
  } else if (blocker) {
    caption = mobileCaption = blockerCaption(blocker.code, blocker.message, {
      live,
      p: label,
    });
    need = true;
  } else if (!live) {
    const base = turnOnIntent ? turnOnCaption(label) : APPLY_NEXT;
    caption = base + (unlinked ? unlinkedNote(label) : "");
    mobileCaption = base + (unlinked ? unlinkedNoteShort : "");
    need = unlinked;
  } else if (dirty) {
    const relink =
      preview.issuer_change && verdict?.kind !== "owner_linked"
        ? relinkNote
        : "";
    caption = mobileCaption = APPLY_NEXT + relink;
  } else {
    caption = mobileCaption = NOTHING_TO_SAVE;
  }
  const canSave =
    settled && !failed && Boolean(preview) && !blocker && (live ? dirty : true);

  const showChanges =
    Boolean(preview) && !readOnly && ((live && dirty) || (!live && (preview?.people.length ?? 0) > 0));
  const changes = showChanges ? changeCount(preview) : 0;

  const ready =
    discovery.state === "ok" &&
    Boolean(form.provider.client_id.trim()) &&
    Boolean(form.provider.client_secret || snapshot.provider.client_secret_saved);
  const laterEnabled =
    dirty &&
    discovery.state === "ok" &&
    Boolean(form.provider.client_id.trim());

  const groupsMissingSince =
    snapshot.provider.groups_missing && snapshot.provider.tested_at
      ? sinceText(snapshot.provider.tested_at)
      : tested && test.passed?.noGroups
        ? "today"
        : null;

  const testedWhen = testedAt ? formatWhen(testedAt) : null;
  const mobileFooter =
    canSave && changes > 0 ? (
      <>
        {changes} {changes === 1 ? "change" : "changes"} ·{" "}
        <a className="text-link underline underline-offset-4" href="#sign-in-changes">
          Review
        </a>
      </>
    ) : (
      mobileCaption
    );

  const saveError = save.error ? (
    save.error instanceof ApiError && save.error.code === "settings_conflict" ? (
      <span className="flex flex-wrap items-center gap-3">
        Sign-in settings changed elsewhere. Reload to see the latest.
        <Button variant="outline" onClick={() => void reload()}>
          Reload
        </Button>
      </span>
    ) : save.error instanceof ApiError && save.error.code === "owner_required" ? (
      READ_ONLY_NOTE
    ) : save.error instanceof Error ? (
      save.error.message
    ) : (
      "Changes could not be saved. Try again."
    )
  ) : null;

  return (
    <ReadOnlyProvider value={readOnly}>
      <div className="space-y-8">
        {readOnly && (
          <p role="status" className="text-muted-foreground text-meta">
            {READ_ONLY_NOTE}
          </p>
        )}
        <WaysToSignIn
          label={label}
          password={form.methods.password}
          provider={form.methods.provider}
          providerStatus={providerStatusCopy({
            formOn: form.methods.provider,
            tested,
            liveUnchanged,
            savedTested,
            testedAt,
            liveBefore: live,
            hasConnection: hasSavedConnection(snapshot),
            freshLogin,
          })}
          hasAddress={
            discovery.state === "ok" || hasSavedConnection(snapshot)
          }
          canTurnOnProvider={tested || liveUnchanged || savedTested}
          freshWarning={freshWarning}
          owner={snapshot.owner}
          recoveryCommand={snapshot.recovery_command}
          onPassword={(password) =>
            setForm((f) => ({ ...f, methods: { ...f.methods, password } }))
          }
          onProvider={(provider) =>
            setForm((f) => ({ ...f, methods: { ...f.methods, provider } }))
          }
          onSnapshot={replaceSnapshot}
        />
        <ConnectionBlock
          connection={form.provider}
          label={label}
          redirectUri={snapshot.provider.redirect_uri}
          secretSaved={snapshot.provider.client_secret_saved}
          discovery={discovery.state}
          found={discovery.found}
          open={connectionOpen}
          onOpenChange={setConnectionOpen}
          advancedOpen={advancedOpen}
          onAdvancedOpenChange={setAdvancedOpen}
          testedWhen={testedWhen}
          groupsDetected={Boolean(testedAt)}
          onChange={patchConnection}
        >
          {!readOnly && (
            <TestSignIn
              label={label}
              ready={ready}
              addressBad={discovery.state === "bad"}
              waiting={test.waiting}
              tested={tested || liveUnchanged || savedTested}
              testedWhen={testedWhen ?? ""}
              changedSinceLastTest={live && !unchanged}
              result={test.result}
              verdict={
                test.result?.kind === "pass" && test.result.testId === testId
                  ? verdict
                  : null
              }
              checkingVerdict={Boolean(testId) && !settled}
              rules={form.rules.rules}
              plans={snapshot.plans}
              justLinked={
                test.result?.kind === "pass" &&
                linkedTestId === test.result.testId
              }
              linking={link.isPending}
              connectionOpen={connectionOpen}
              onTest={() => test.start(form.provider)}
              onCancel={test.cancel}
              onDismiss={test.dismiss}
              onLink={(id) => link.mutate(id)}
              onEditConnection={() => setConnectionOpen(true)}
            />
          )}
        </ConnectionBlock>
        <RulesEditor
          rules={form.rules}
          saved={snapshot.rules}
          label={label}
          plans={snapshot.plans}
          seen={snapshot.seen}
          ruleMatches={snapshot.rule_matches}
          previewMatches={preview?.rule_matches ?? null}
          groupsMissingSince={groupsMissingSince}
          notice={readOnly ? null : snapshot.notice}
          disabled={readOnly}
          phone={phone}
          onChange={setRules}
          onDismissNotice={() => dismiss.mutate()}
        />
        <SharedComputers
          value={form.memory ?? "remember"}
          onChange={(memory) => setForm((f) => ({ ...f, memory }))}
        />
        {showChanges && preview && (
          <WhatChanges
            preview={preview}
            pending={!settled}
            label={label}
            keep={keep}
            disabled={readOnly}
            onKeep={(id, on) => setKeep((k) => ({ ...k, [id]: on }))}
          />
        )}
        {!readOnly && (
          <SaveFooter
            caption={phone ? mobileFooter : caption}
            need={need}
            canSave={canSave}
            saving={save.isPending}
            saveLabel={turnOnIntent ? `Turn on ${mid(label)}` : "Save changes"}
            showDiscard={live && dirty}
            later={
              live ? null : { enabled: laterEnabled && !save.isPending }
            }
            error={saveError}
            onSave={() => save.mutate(false)}
            onLater={() => save.mutate(true)}
            onDiscard={discard}
          />
        )}
      </div>
    </ReadOnlyProvider>
  );
}
