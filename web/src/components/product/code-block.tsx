"use client";
import { useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useCopied } from "@/components/ui/copy-button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export interface CodeSnippet {
  language: string;
  code: string;
}
export interface CodeBlockProps {
  code?: string;
  language?: string;
  snippets?: CodeSnippet[];
}
export function CodeBlock({
  code = "",
  language = "Text",
  snippets,
}: CodeBlockProps) {
  const values = snippets?.length ? snippets : [{ code, language }];
  const [selectedLanguage, setSelectedLanguage] = useState(values[0].language);
  const activeLanguage = values.some((snippet) => snippet.language === selectedLanguage)
    ? selectedLanguage
    : values[0].language;
  const { copied, failed, copy } = useCopied(JSON.stringify([activeLanguage, values]));
  return (
    <Tabs
      value={activeLanguage}
      onValueChange={setSelectedLanguage}
      className="bg-muted min-w-0 gap-0 rounded-lg"
    >
      <div className="overflow-x-auto border-b px-3 pt-2">
        <TabsList aria-label="Code language">
          {values.map((s) => (
            <TabsTrigger key={s.language} value={s.language}>
              {s.language}
            </TabsTrigger>
          ))}
        </TabsList>
      </div>
      {values.map((s) => (
        <TabsContent
          key={s.language}
          value={s.language}
          className="relative min-w-0 p-4"
        >
          <Button
            type="button"
            variant="ghost"
            size="icon-sm"
            className="absolute top-2 right-2"
            disabled={!s.code}
            onClick={() => void copy(s.code)}
            aria-label={`Copy ${s.language} code`}
          >
            {copied && activeLanguage === s.language ? <Check /> : <Copy />}
          </Button>
          {s.code ? (
            <pre className="overflow-x-auto pr-8 font-mono text-xs leading-relaxed">
              <code>{s.code}</code>
            </pre>
          ) : (
            <p className="text-muted-foreground text-sm">
              No example available yet.
            </p>
          )}
        </TabsContent>
      ))}
      <span role="status" className="sr-only">
        {copied ? "Code copied" : ""}
      </span>
      {failed && (
        <p role="alert" className="text-destructive p-3 text-sm">
          {"Couldn't copy. Select the text and copy it yourself."}
        </p>
      )}
    </Tabs>
  );
}
