"use client";

import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { KeyValueList } from "@/components/product/key-value";
import { Button } from "@/components/ui/button";
import { accountApi } from "@/lib/account-api";
import { formatNumber } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { Fact, Reply, RequestContent, Turn } from "@/lib/request-content";

/**
 * What a kept request's content reads as, by its kind: chat bubbles for a conversation, the prompt and settings for a
 * picture, sound or video, the file and transcript for a transcription, the input and a count for embeddings, and a
 * plain list of facts for a kind the screens do not know.
 */
export function RequestBody({
  content,
  requestId,
}: {
  content: RequestContent;
  requestId: string | null;
}) {
  if (content.shape === "conversation")
    return <Conversation turns={content.turns} reply={content.reply} />;
  if (content.shape === "media")
    return (
      <Media
        requestId={requestId}
        prompt={content.prompt}
        facts={content.facts}
        failure={content.failure}
      />
    );
  if (content.shape === "transcription")
    return (
      <Transcription
        file={content.file}
        transcript={content.transcript}
        failure={content.failure}
      />
    );
  if (content.shape === "embeddings")
    return (
      <Embeddings
        inputs={content.inputs}
        numbers={content.numbers}
        failure={content.failure}
      />
    );
  return <Facts facts={content.facts} failure={content.failure} />;
}

/** The conversation as bubbles, the person's on the right and the model's on the left, then the reply. */
function Conversation({ turns, reply }: { turns: Turn[]; reply: Reply }) {
  return (
    <ol aria-label="Conversation" className="space-y-3">
      {turns.map((turn, index) => (
        <li key={index} className="flex flex-col gap-1">
          <TurnView turn={turn} />
        </li>
      ))}
      <li className="flex flex-col items-start gap-1">
        <ReplyView reply={reply} />
      </li>
    </ol>
  );
}

function TurnView({ turn }: { turn: Turn }) {
  const mine = turn.role === "user";
  return (
    <div className={cn("flex flex-col gap-1", mine ? "items-end" : "items-start")}>
      {turn.text ? <Bubble mine={mine}>{turn.text}</Bubble> : null}
      <ToolLines lines={turn.lines} />
    </div>
  );
}

function ReplyView({ reply }: { reply: Reply }) {
  return (
    <>
      {reply.failure ? <p className="text-sm font-medium">{reply.failure}</p> : null}
      {reply.kept ? null : <p className="text-muted-foreground text-meta">The reply wasn’t kept.</p>}
      {reply.text ? <Bubble mine={false}>{reply.text}</Bubble> : null}
      <ToolLines lines={reply.lines} />
    </>
  );
}

/** One message's words. The screen reader hears who said them; the sight reader sees only the words. */
function Bubble({ mine, children }: { mine: boolean; children: string }) {
  return (
    <p
      className={cn(
        "max-w-[85%] rounded-2xl px-3 py-2 text-sm break-words whitespace-pre-wrap",
        mine ? "bg-muted" : "border bg-background",
      )}
    >
      <span className="sr-only">{mine ? "You: " : "Model: "}</span>
      <span>{children}</span>
    </p>
  );
}

function ToolLines({ lines }: { lines: string[] }) {
  return (
    <>
      {lines.map((line, index) => (
        <p key={index} className="text-muted-foreground text-meta">
          {line}
        </p>
      ))}
    </>
  );
}

/** The prompt and the settings a picture, sound or video was made with, and a link to its Library item when there is one. */
function Media({
  requestId,
  prompt,
  facts,
  failure,
}: {
  requestId: string | null;
  prompt: string | null;
  facts: Fact[];
  failure: string | null;
}) {
  const { data: libraryAsset } = useQuery({
    queryKey: ["account", "request-history", "library", requestId, prompt],
    queryFn: () => accountApi.libraryItemForRequest(requestId ?? "", prompt ?? ""),
    enabled: Boolean(requestId && prompt),
    retry: false,
  });
  return (
    <div className="space-y-4">
      <KeyValueList items={[...facts, ...resultFacts(failure)]} />
      {libraryAsset ? (
        <Button asChild variant="outline" size="sm">
          <Link prefetch={false} href={`/library?asset=${encodeURIComponent(libraryAsset)}`}>
            Open in Library
          </Link>
        </Button>
      ) : null}
    </div>
  );
}

/** The file's type and size, then the transcript. */
function Transcription({
  file,
  transcript,
  failure,
}: {
  file: Fact[];
  transcript: string | null;
  failure: string | null;
}) {
  return (
    <div className="space-y-4">
      <KeyValueList items={[...file, ...resultFacts(failure)]} />
      <section aria-labelledby="history-transcript" className="space-y-2">
        <h3 id="history-transcript" className="text-sm font-medium">
          Transcript
        </h3>
        {transcript ? (
          <p className="text-sm break-words whitespace-pre-wrap">{transcript}</p>
        ) : (
          <p className="text-muted-foreground text-meta">No transcript was kept.</p>
        )}
      </section>
    </div>
  );
}

/** The input text, and how many numbers came back. */
function Embeddings({
  inputs,
  numbers,
  failure,
}: {
  inputs: string[];
  numbers: number | null;
  failure: string | null;
}) {
  return (
    <div className="space-y-4">
      <section aria-labelledby="history-input" className="space-y-2">
        <h3 id="history-input" className="text-sm font-medium">
          Input
        </h3>
        {inputs.length ? (
          inputs.map((text, index) => (
            <p key={index} className="text-sm break-words whitespace-pre-wrap">
              {text}
            </p>
          ))
        ) : (
          <p className="text-muted-foreground text-meta">No text was kept.</p>
        )}
      </section>
      <p className="text-sm">{numbersWords(numbers)}</p>
      {failure ? <p className="text-sm font-medium">{failure}</p> : null}
    </div>
  );
}

/** The facts of a request whose kind the screens do not know. */
function Facts({ facts, failure }: { facts: Fact[]; failure: string | null }) {
  const items = [...facts, ...resultFacts(failure)];
  return items.length ? (
    <KeyValueList items={items} />
  ) : (
    <p className="text-muted-foreground text-meta">
      Nothing readable was kept. Open Technical details to see the request as it was kept.
    </p>
  );
}

function resultFacts(failure: string | null): Fact[] {
  return failure ? [["Result", failure]] : [];
}

/** "1 number returned", "1,536 numbers returned"; the words say when the numbers were not kept. */
function numbersWords(numbers: number | null): string {
  if (numbers === null) return "The numbers weren’t kept.";
  return numbers === 1 ? "1 number returned" : `${formatNumber(numbers)} numbers returned`;
}
