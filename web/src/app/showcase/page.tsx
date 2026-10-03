"use client";

import { useQuery } from "@tanstack/react-query";
import { ImageIcon } from "lucide-react";
import { PageHeader } from "@/components/product/page-header";
import { ErrorState } from "@/components/product/error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

type Output = { kind: string; file?: string; text?: string; label?: string };
type Example = {
  id: string;
  base: string;
  card: string;
  title?: string;
  model?: string;
  seconds?: number;
  inputs?: { label?: string; value?: string }[];
  outputs?: Output[];
};
type Section = {
  id: string;
  title: string;
  source?: string;
  fallback?: boolean;
  match?: { intent?: string; id?: string; not_id?: string };
};

function belongsTo(example: Example, sections: Section[]): string | undefined {
  const aliases: Record<string, string> = {
    chat: "aliases:chat",
    vision: "aliases:chat",
    tts: "aliases:speech",
    voices: "aliases:speech",
    "custom-voices": "aliases:speech",
    stt: "aliases:transcriptions",
    "stt-diarize": "aliases:transcriptions",
    embeddings: "aliases:embeddings",
  };
  const source = example.card.startsWith("protocol-")
    ? "protocols"
    : aliases[example.card];
  if (source) return sections.find((section) => section.source === source)?.id;
  const [modality, operation, ...name] = example.card.split(".");
  const intent = `${modality}.${operation}`;
  const shortName = name.join(".");
  return (
    sections.find(
      ({ match }) =>
        match &&
        (!match.intent || match.intent === intent) &&
        (!match.id || new RegExp(match.id).test(shortName)) &&
        (!match.not_id || !new RegExp(match.not_id).test(shortName)),
    )?.id ?? sections.find((section) => section.fallback)?.id
  );
}

function mediaUrl(example: Example, file?: string): string | undefined {
  if (
    !file ||
    !/^[A-Za-z0-9_.-]+$/.test(file) ||
    !/^[a-z0-9][a-z0-9._-]{0,95}$/.test(example.id)
  )
    return undefined;
  return `/showcase/examples/${example.id}/${file}`;
}

function RecordedOutput({
  example,
  output,
}: {
  example: Example;
  output: Output;
}) {
  const src = mediaUrl(example, output.file);
  if (output.kind === "image" && src) {
    // Recorded media dimensions vary; keep native images within the card.
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={src}
        alt={output.label || example.title || "Recorded image output"}
        loading="lazy"
        className="bg-muted max-h-96 w-full rounded-md object-contain"
      />
    );
  }
  if (output.kind === "audio" && src)
    return (
      <audio
        controls
        preload="none"
        src={src}
        aria-label={output.label || "Recorded audio output"}
        className="w-full min-w-0"
      />
    );
  if (output.kind === "video" && src)
    return (
      <div className="space-y-2">
        {/* The recorded-example contract supplies no caption file. */}
        <video
          controls
          preload="metadata"
          src={src}
          aria-label={output.label || "Recorded video output"}
          className="w-full rounded-md"
        />
        <p className="text-muted-foreground text-xs">
          Captions are unavailable for this recording.
        </p>
      </div>
    );
  if (typeof output.text === "string")
    return (
      <pre className="bg-muted max-h-96 overflow-y-auto rounded-md p-4 text-sm [overflow-wrap:anywhere] whitespace-pre-wrap">
        {output.text}
      </pre>
    );
  return null;
}

export default function GalleryPage() {
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["public", "recorded-gallery"],
    queryFn: async () => {
      const responses = await Promise.all([
        fetch("/showcase/examples/index.json"),
        fetch("/showcase/static/sections.json"),
      ]);
      if (responses.some((response) => !response.ok))
        throw new Error("Recorded examples could not be loaded");
      const [examples, sections] = await Promise.all(
        responses.map((response) => response.json()),
      );
      if (!Array.isArray(examples.data) || !Array.isArray(sections.sections)) {
        throw new Error("Recorded example data is invalid");
      }
      return {
        examples: (examples.data as Example[]).filter(
          (example) =>
            typeof example.card === "string" &&
            Array.isArray(example.outputs) &&
            example.outputs.length > 0,
        ),
        sections: sections.sections as Section[],
      };
    },
  });
  return (
    <div className="mx-auto max-w-5xl space-y-8 py-6">
      <PageHeader
        title="Gallery"
        answer="Real examples recorded on this gateway."
        description="Browse saved outputs. These examples show past requests, not current model availability."
      />
      {isPending ? (
        <div
          aria-label="Loading recorded examples"
          className="grid gap-6 sm:grid-cols-2"
        >
          <Skeleton className="h-64 w-full" />
          <Skeleton className="h-64 w-full" />
        </div>
      ) : isError ? (
        <ErrorState
          message="Recorded examples could not be loaded. Check your connection and try again."
          onRetry={() => void refetch()}
          retrying={isFetching}
        />
      ) : data.examples.length === 0 ? (
        <EmptyState icon={<ImageIcon aria-hidden="true" className="size-8" />}>
          No recorded examples yet. The gateway owner can record examples to
          share them here.
        </EmptyState>
      ) : (
        data.sections.map((section) => {
          const examples = data.examples.filter(
            (example) =>
              belongsTo(example, data.sections) === section.id &&
              (example.outputs?.length ?? 0) > 0,
          );
          if (!examples.length) return null;
          return (
            <section
              key={section.id}
              aria-labelledby={`gallery-${section.id}`}
              className="space-y-4"
            >
              <h2
                id={`gallery-${section.id}`}
                className="text-heading font-semibold"
              >
                {section.title}
              </h2>
              <div className="grid items-start gap-6 sm:grid-cols-2">
                {examples.map((example) => (
                  <Card key={example.id} className="min-w-0">
                    <CardHeader>
                      <h3 className="font-medium [overflow-wrap:anywhere]">
                        {example.title || "Recorded example"}
                      </h3>
                      <div className="text-muted-foreground flex flex-wrap items-center gap-2 text-xs">
                        <Badge variant="secondary">Recorded</Badge>
                        {example.model && (
                          <span className="[overflow-wrap:anywhere]">
                            {example.model}
                          </span>
                        )}
                        {typeof example.seconds === "number" &&
                          Number.isFinite(example.seconds) && (
                            <span>{example.seconds.toFixed(1)} seconds</span>
                          )}
                      </div>
                    </CardHeader>
                    <CardContent className="min-w-0 space-y-4">
                      {example.outputs?.map((output, index) => (
                        <RecordedOutput
                          key={index}
                          example={example}
                          output={output}
                        />
                      ))}
                      <details className="min-w-0">
                        <summary className="text-muted-foreground min-h-11 cursor-pointer py-3 text-sm">
                          Recorded input
                        </summary>
                        {example.inputs
                          ?.filter((input) => input.value)
                          .map((input, index) => (
                            <p
                              key={index}
                              className="text-muted-foreground pb-3 text-sm [overflow-wrap:anywhere]"
                            >
                              {input.value}
                            </p>
                          ))}
                      </details>
                    </CardContent>
                  </Card>
                ))}
              </div>
            </section>
          );
        })
      )}
    </div>
  );
}
