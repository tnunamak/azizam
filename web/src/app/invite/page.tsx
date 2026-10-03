"use client";
import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Fish, ArrowRight, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  ErrorState,
  EmptyState,
  RelativeTime,
  SecretReveal,
} from "@/components/product";
import { type ProfilePolicyDocument } from "@/lib/api";
import { authFetch, continueTo, type SignInOptions } from "../sign-in/auth-api";
import { planSummary } from "../people/people-api";
interface Invitation {
  plan: {
    id: string;
    name: string;
    description: string | null;
    profile: ProfilePolicyDocument;
  };
  email: string | null;
  expires_at: number;
  owner_name: string;
  instance_name: string;
}
export default function InvitePage() {
  const [token, setToken] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  useEffect(() => {
    setToken(
      new URLSearchParams(window.location.hash.slice(1)).get("token") ||
        new URLSearchParams(window.location.search).get("token") ||
        sessionStorage.getItem("gateway-invitation") ||
        "",
    );
  }, []);
  const {
    data: optionsData,
    isPending: optionsIsPending,
    isError: optionsIsError,
    refetch: optionsRefetch,
  } = useQuery({
    queryKey: ["auth", "options"],
    queryFn: () => authFetch<SignInOptions>("/auth/api/options"),
  });
  const {
    data: inviteData,
    isPending: inviteIsPending,
    isError: inviteIsError,
    error: inviteError,
    refetch: inviteRefetch,
  } = useQuery({
    queryKey: ["auth", "invite", token],
    queryFn: () =>
      authFetch<Invitation>(
        `/auth/api/invite?token=${encodeURIComponent(token || "")}`,
      ),
    enabled: !!token,
    retry: false,
  });
  const accept = useMutation({
    mutationFn: (oidc: boolean = false) =>
      authFetch<{ next: string; credential: { api_key: string } }>(
        "/auth/api/invite/accept",
        oidc
          ? {
              token,
              name: optionsData?.signed_in_oidc?.name,
              email: optionsData?.signed_in_oidc?.email,
            }
          : { token, name, email: inviteData?.email || email, password },
        oidc
          ? {
              "x-ai-gateway-csrf":
                optionsData?.signed_in_oidc?.csrf_token || "",
            }
          : undefined,
      ),
    onSuccess: () => {
      setPassword("");
      sessionStorage.removeItem("gateway-invitation");
      window.history.replaceState(null, "", window.location.pathname);
    },
  });
  return (
    <section className="mx-auto w-full max-w-md py-12 sm:py-16">
      <div className="bg-card rounded-xl border p-6 sm:p-8">
        <Fish className="text-primary mb-6 size-8" aria-hidden="true" />
        <h1 className="text-2xl font-semibold tracking-tight">
          {accept.data ? "Your account is ready" : "Accept your invitation"}
        </h1>
        {accept.data ? (
          <div className="mt-6 space-y-6">
            <p className="text-muted-foreground text-sm">
              Your first app and key are ready. Save your key, then connect an
              app from Home.
            </p>
            <SecretReveal
              secret={accept.data.credential.api_key}
              onSaved={() => {
                const next = accept.data?.next || "/ui/";
                accept.reset();
                continueTo(next);
              }}
            />
          </div>
        ) : token === null || (!!token && inviteIsPending) ? (
          <div
            role="status"
            className="mt-6 space-y-4"
            aria-label="Loading invitation"
          >
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-40 w-full" />
          </div>
        ) : !token ? (
          <div className="mt-6">
            <EmptyState
              title="You need an invite link"
              description="Ask the gateway owner for an invitation, then open the complete link they share."
              action={
                <Button asChild variant="outline">
                  <a href="/ui/home/">Return to gateway home</a>
                </Button>
              }
            />
          </div>
        ) : inviteIsError ? (
          <div className="mt-6">
            <ErrorState
              message={inviteError.message}
              onRetry={() => void inviteRefetch()}
            />
          </div>
        ) : (
          inviteData && (
            <>
              <p className="text-muted-foreground mt-2 text-sm">
                {inviteData.owner_name} invited you to{" "}
                {inviteData.instance_name}.
              </p>
              <div className="bg-muted my-6 rounded-lg p-4">
                <p className="flex items-center gap-2 text-sm font-medium">
                  <ShieldCheck className="size-4" aria-hidden="true" />
                  {inviteData.plan.name}
                </p>
                <p className="text-muted-foreground mt-2 text-xs leading-5 break-words">
                  {planSummary(inviteData.plan.profile)}
                </p>
                <p className="text-muted-foreground mt-3 text-xs">
                  Invite expires <RelativeTime ts={inviteData.expires_at} />
                </p>
              </div>
              {optionsIsPending ? (
                <p role="status">Loading sign-in options…</p>
              ) : optionsIsError ? (
                <ErrorState
                  message="Sign-in options could not be loaded."
                  onRetry={() => void optionsRefetch()}
                />
              ) : optionsData?.signed_in_oidc ? (
                <div className="space-y-4">
                  <p className="text-muted-foreground text-sm">
                    Accept as {optionsData.signed_in_oidc.email}.
                  </p>
                  {accept.isError && (
                    <p role="alert" className="text-destructive text-sm">
                      {accept.error.message}
                    </p>
                  )}
                  <Button
                    className="w-full"
                    disabled={accept.isPending}
                    onClick={() => accept.mutate(true)}
                  >
                    {accept.isPending ? "Joining…" : "Accept invitation"}
                  </Button>
                </div>
              ) : (
                <>
                  {optionsData?.built_in && (
                    <form
                      className="space-y-4"
                      onSubmit={(e) => {
                        e.preventDefault();
                        accept.mutate(false);
                      }}
                    >
                      <div className="space-y-2">
                        <Label htmlFor="name">Your name</Label>
                        <Input
                          id="name"
                          autoComplete="name"
                          required
                          value={name}
                          onChange={(e) => setName(e.target.value)}
                          disabled={accept.isPending}
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="email">Email</Label>
                        <Input
                          id="email"
                          type="email"
                          autoComplete="username"
                          required
                          value={inviteData.email || email}
                          readOnly={!!inviteData.email}
                          onChange={(e) => setEmail(e.target.value)}
                          disabled={accept.isPending}
                        />
                      </div>
                      <div className="space-y-2">
                        <Label htmlFor="password">Password</Label>
                        <Input
                          id="password"
                          type="password"
                          autoComplete="new-password"
                          required
                          minLength={12}
                          value={password}
                          onChange={(e) => setPassword(e.target.value)}
                          disabled={accept.isPending}
                        />
                        <p className="text-muted-foreground text-xs">
                          Use at least 12 characters.
                        </p>
                      </div>
                      {accept.isError && (
                        <p role="alert" className="text-destructive text-sm">
                          {accept.error.message}
                        </p>
                      )}
                      <Button className="w-full" disabled={accept.isPending}>
                        {accept.isPending
                          ? "Creating your account…"
                          : "Create account"}
                        <ArrowRight />
                      </Button>
                    </form>
                  )}
                  {optionsData?.oidc && (
                    <div
                      className={
                        optionsData.built_in ? "mt-6 border-t pt-6" : ""
                      }
                    >
                      <Button
                        variant="outline"
                        className="w-full"
                        onClick={() => {
                          sessionStorage.setItem("gateway-invitation", token);
                          window.location.assign(
                            "/admin/login?return_to=/ui/invite/",
                          );
                        }}
                      >
                        Continue with{" "}
                        {optionsData.oidc_label || "your identity provider"}
                      </Button>
                    </div>
                  )}
                  {!optionsData?.built_in && !optionsData?.oidc && (
                    <EmptyState
                      title="Sign-in is not configured"
                      description="Ask the gateway owner to enable accounts or an identity provider."
                      action={
                        <Button asChild variant="outline">
                          <a href="/ui/">Return to gateway home</a>
                        </Button>
                      }
                    />
                  )}
                </>
              )}
            </>
          )
        )}
      </div>
    </section>
  );
}
