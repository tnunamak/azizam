"use client";
import { useId, useState, type ReactNode } from "react";
import { useTheme } from "next-themes";
import { toast } from "sonner";
import { Loader2, Plus, Search, Sun, Moon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Combobox } from "@/components/ui/combobox";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { Checkbox } from "@/components/ui/checkbox";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Progress } from "@/components/ui/progress";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  CapabilityIcon,
  CodeBlock,
  CopyField,
  EmptyState,
  EntityName,
  ErrorState,
  Money,
  PageHeader,
  RelativeTime,
  SecretReveal,
  StatusPill,
  type Capability,
} from "@/components/product";

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex min-w-0 flex-col gap-5 border-t pt-8">
      <h2 className="text-lg font-semibold tracking-tight">{title}</h2>
      {children}
    </section>
  );
}
function State({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <h3 className="text-muted-foreground text-xs font-medium">{title}</h3>
      {children}
    </div>
  );
}
const grid = "grid min-w-0 gap-5 sm:grid-cols-2 lg:grid-cols-3";
export default function DesignPage() {
  const { setTheme } = useTheme();
  const [model, setModel] = useState("");
  const [retrying, setRetrying] = useState(false);
  const id = useId();
  const options = [
    { value: "chat", label: "Chat" },
    { value: "image", label: "Images" },
    { value: "video", label: "Video", disabled: true },
  ];
  return (
    <TooltipProvider>
      <div className="flex min-w-0 flex-col gap-8">
        <PageHeader
          title="Design system"
          description="Building blocks for your gateway. These examples are not live gateway data."
          actions={
            <>
              <Button variant="outline" onClick={() => setTheme("light")}>
                <Sun data-icon="inline-start" />
                Light
              </Button>
              <Button variant="outline" onClick={() => setTheme("dark")}>
                <Moon data-icon="inline-start" />
                Dark
              </Button>
            </>
          }
        />
        <p className="text-muted-foreground text-sm">
          Use Tab to inspect focus states. Hover controls to inspect feedback.
          Light and Dark apply to every example, including overlays.
        </p>
        <Section title="Actions">
          <div className="flex flex-wrap items-end gap-4">
            <State title="Default">
              <Button>
                <Plus data-icon="inline-start" />
                New app
              </Button>
            </State>
            {(
              ["outline", "secondary", "ghost", "destructive", "link"] as const
            ).map((variant) => (
              <State key={variant} title={variant}>
                <Button variant={variant}>
                  {variant === "destructive" ? "Revoke key" : "View details"}
                </Button>
              </State>
            ))}
            <State title="Disabled">
              <Button disabled>Unavailable</Button>
            </State>
            <State title="Loading">
              <Button disabled aria-busy>
                <Loader2 className="animate-spin" data-icon="inline-start" />
                Saving…
              </Button>
            </State>
            <State title="Icon">
              <Button variant="outline" size="icon" aria-label="Search">
                <Search />
              </Button>
            </State>
            <State title="Small">
              <Button size="sm">Copy alias</Button>
            </State>
            <State title="Large">
              <Button size="lg">Continue</Button>
            </State>
          </div>
        </Section>
        <Section title="Text fields">
          <div className={grid}>
            {(["Default", "Filled", "Disabled", "Error"] as const).map(
              (state) => (
                <State key={state} title={`Input · ${state}`}>
                  <Label htmlFor={`${id}-input-${state}`}>App name</Label>
                  <Input
                    id={`${id}-input-${state}`}
                    placeholder="Your app"
                    defaultValue={
                      state === "Filled" || state === "Disabled"
                        ? "My coding assistant"
                        : undefined
                    }
                    disabled={state === "Disabled"}
                    aria-invalid={state === "Error"}
                    aria-describedby={
                      state === "Error" ? `${id}-input-help` : undefined
                    }
                  />
                  {state === "Error" && (
                    <p
                      id={`${id}-input-help`}
                      className="text-destructive text-xs"
                    >
                      Enter an app name.
                    </p>
                  )}
                </State>
              ),
            )}
            <State title="Input · loading">
              <Skeleton className="h-9 w-full" />
              <span role="status" className="sr-only">
                Loading app name
              </span>
            </State>
            {(["Default", "Disabled", "Error"] as const).map((state) => (
              <State key={state} title={`Textarea · ${state}`}>
                <Label htmlFor={`${id}-textarea-${state}`}>Prompt</Label>
                <Textarea
                  id={`${id}-textarea-${state}`}
                  placeholder="What would you like to make?"
                  disabled={state === "Disabled"}
                  aria-invalid={state === "Error"}
                  aria-describedby={
                    state === "Error" ? `${id}-textarea-help` : undefined
                  }
                />
                {state === "Error" && (
                  <p
                    id={`${id}-textarea-help`}
                    className="text-destructive text-xs"
                  >
                    Enter a prompt.
                  </p>
                )}
              </State>
            ))}
          </div>
        </Section>
        <Section title="Selection">
          <div className={grid}>
            {(["Default", "Disabled", "Error"] as const).map((state) => (
              <State key={state} title={`Select · ${state}`}>
                <Select disabled={state === "Disabled"}>
                  <SelectTrigger
                    aria-label={`${state} model`}
                    aria-invalid={state === "Error"}
                  >
                    <SelectValue placeholder="Choose a model" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectGroup>
                      <SelectItem value="chat">Chat</SelectItem>
                      <SelectItem value="image">Images</SelectItem>
                      <SelectItem value="video" disabled>
                        Video unavailable
                      </SelectItem>
                    </SelectGroup>
                  </SelectContent>
                </Select>
                {state === "Error" && (
                  <p className="text-destructive text-xs">Choose a model.</p>
                )}
              </State>
            ))}
            <State title="Combobox · default">
              <Combobox
                label="Model"
                options={options}
                value={model}
                onValueChange={setModel}
              />
            </State>
            <State title="Combobox · empty">
              <Combobox
                label="Empty models"
                options={[]}
                onValueChange={setModel}
              />
            </State>
            <State title="Combobox · loading">
              <Combobox
                label="Loading models"
                options={[]}
                loading
                onValueChange={setModel}
              />
            </State>
            <State title="Combobox · disabled">
              <Combobox
                label="Disabled models"
                options={options}
                disabled
                onValueChange={setModel}
              />
            </State>
            <State title="Combobox · error">
              <Combobox
                label="Unavailable models"
                options={[]}
                error="Models could not be loaded. Try again."
                onValueChange={setModel}
              />
            </State>
            <State title="Switch">
              <Label>
                <Switch defaultChecked />
                Enable access
              </Label>
              <Label>
                <Switch />
                Off
              </Label>
              <Label>
                <Switch disabled />
                Disabled
              </Label>
            </State>
            <State title="Checkbox">
              <Label>
                <Checkbox defaultChecked />
                Local models
              </Label>
              <Label>
                <Checkbox defaultChecked="indeterminate" />
                Some models
              </Label>
              <Label>
                <Checkbox disabled />
                Paid providers disabled
              </Label>
              <Label>
                <Checkbox aria-invalid />
                Selection needed
              </Label>
            </State>
            <State title="Radio group">
              <RadioGroup aria-label="Model location" defaultValue="local">
                <Label>
                  <RadioGroupItem value="local" />
                  Local models
                </Label>
                <Label>
                  <RadioGroupItem value="remote" />
                  Remote providers
                </Label>
                <Label>
                  <RadioGroupItem value="disabled" disabled />
                  Unavailable
                </Label>
              </RadioGroup>
            </State>
          </div>
        </Section>
        <Section title="Overlays">
          <p className="text-muted-foreground text-sm">
            Open an example. Escape closes it and focus returns to the trigger.
          </p>
          <div className="flex flex-wrap gap-3">
            <Dialog>
              <DialogTrigger asChild>
                <Button variant="outline">Open dialog</Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Create an app</DialogTitle>
                  <DialogDescription>
                    Give each client its own key and access plan.
                  </DialogDescription>
                </DialogHeader>
                <Label htmlFor={`${id}-dialog-name`}>App name</Label>
                <Input id={`${id}-dialog-name`} placeholder="My app" />
                <Button onClick={() => toast.success("Example app created")}>
                  Create example
                </Button>
              </DialogContent>
            </Dialog>
            <Sheet>
              <SheetTrigger asChild>
                <Button variant="outline">Open sheet</Button>
              </SheetTrigger>
              <SheetContent>
                <SheetHeader>
                  <SheetTitle>Request details</SheetTitle>
                  <SheetDescription>
                    One clear answer, with technical detail when you need it.
                  </SheetDescription>
                </SheetHeader>
                <div className="flex flex-col gap-4 p-4">
                  <StatusPill
                    status="ready"
                    reason="The example request succeeded."
                  />
                  <CodeBlock
                    code={'{ "status": "succeeded" }'}
                    language="JSON"
                  />
                </div>
              </SheetContent>
            </Sheet>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="outline">App actions</Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent>
                <DropdownMenuGroup>
                  <DropdownMenuItem
                    onSelect={() => toast.success("Example key created")}
                  >
                    Create key
                  </DropdownMenuItem>
                  <DropdownMenuItem disabled>
                    Rotate unavailable key
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    variant="destructive"
                    onSelect={() =>
                      toast.error("Example error: could not revoke key")
                    }
                  >
                    Revoke key
                  </DropdownMenuItem>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button variant="outline">Hover or focus</Button>
              </TooltipTrigger>
              <TooltipContent>
                Copy the stable alias to your app.
              </TooltipContent>
            </Tooltip>
            <Popover>
              <PopoverTrigger asChild>
                <Button variant="outline">Open popover</Button>
              </PopoverTrigger>
              <PopoverContent>
                <h3 className="font-medium">Stable aliases</h3>
                <p className="text-muted-foreground mt-2 text-sm">
                  An alias keeps your app connected when a model changes.
                </p>
              </PopoverContent>
            </Popover>
          </div>
        </Section>
        <Section title="Navigation and disclosure">
          <div className="grid min-w-0 gap-8 md:grid-cols-2">
            <Tabs defaultValue="details">
              <TabsList aria-label="Example detail">
                <TabsTrigger value="details">Details</TabsTrigger>
                <TabsTrigger value="empty">Empty</TabsTrigger>
                <TabsTrigger value="error">Error</TabsTrigger>
                <TabsTrigger value="disabled" disabled>
                  Disabled
                </TabsTrigger>
              </TabsList>
              <TabsContent value="details">
                <p className="py-4 text-sm">
                  Details appear without leaving the page.
                </p>
              </TabsContent>
              <TabsContent value="empty">
                <EmptyState
                  title="No requests yet"
                  description="Run a test to see your first receipt."
                />
              </TabsContent>
              <TabsContent value="error">
                <ErrorState onRetry={() => toast.info("Example retry")} />
              </TabsContent>
            </Tabs>
            <Accordion type="single" collapsible>
              <AccordionItem value="advanced">
                <AccordionTrigger>Technical details</AccordionTrigger>
                <AccordionContent>
                  Advanced details stay behind this disclosure.
                </AccordionContent>
              </AccordionItem>
              <AccordionItem value="disabled" disabled>
                <AccordionTrigger>Unavailable section</AccordionTrigger>
                <AccordionContent>Not available.</AccordionContent>
              </AccordionItem>
            </Accordion>
            <Command className="border">
              <CommandInput
                placeholder="Find a page…"
                aria-label="Find a page"
              />
              <CommandList>
                <CommandEmpty>No matching pages.</CommandEmpty>
                <CommandGroup heading="Pages">
                  <CommandItem
                    onSelect={() => toast.info("Example Home navigation")}
                  >
                    Home
                  </CommandItem>
                  <CommandItem
                    onSelect={() => toast.info("Example Models navigation")}
                  >
                    Models
                  </CommandItem>
                  <CommandItem disabled>Unavailable action</CommandItem>
                </CommandGroup>
              </CommandList>
            </Command>
            <ScrollArea
              className="h-36 rounded-md border p-4"
              aria-label="Scrollable example"
            >
              <div className="flex flex-col gap-3">
                {Array.from({ length: 8 }, (_, i) => (
                  <p key={i} className="text-sm">
                    Example row {i + 1}
                  </p>
                ))}
              </div>
            </ScrollArea>
          </div>
        </Section>
        <Section title="Feedback">
          <div className="grid min-w-0 gap-5 sm:grid-cols-2">
            <State title="Badges">
              <div className="flex flex-wrap gap-2">
                <Badge>Primary</Badge>
                <Badge variant="secondary">Local</Badge>
                <Badge variant="outline">Remote</Badge>
                <Badge variant="destructive">Action needed</Badge>
              </div>
            </State>
            <State title="Toast">
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  onClick={() => toast.success("Changes saved")}
                >
                  Success
                </Button>
                <Button
                  variant="outline"
                  onClick={() => toast.error("Couldn’t save. Try again.")}
                >
                  Error
                </Button>
                <Button
                  variant="outline"
                  onClick={() => toast.info("No changes to save")}
                >
                  Info
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    const pending = toast.loading("Saving example…");
                    setTimeout(
                      () => toast.success("Example saved", { id: pending }),
                      1500,
                    );
                  }}
                >
                  Loading
                </Button>
              </div>
            </State>
            <State title="Skeleton">
              <div
                aria-busy="true"
                aria-label="Loading example"
                className="flex items-center gap-3"
              >
                <Skeleton className="size-10 rounded-full" />
                <div className="flex flex-1 flex-col gap-2">
                  <Skeleton className="h-4 w-3/4" />
                  <Skeleton className="h-3 w-1/2" />
                </div>
              </div>
            </State>
            <State title="Progress">
              <Progress value={64} aria-label="Example progress" />
              <p className="text-muted-foreground text-xs">64% complete</p>
              <Progress value={0} aria-label="Not started" />
              <Progress value={100} aria-label="Complete" />
            </State>
            <State title="Separator">
              <div className="flex items-center gap-3 text-sm">
                Local
                <Separator orientation="vertical" className="h-4" />
                Remote
              </div>
              <Separator />
            </State>
            <State title="Avatar">
              <div className="flex gap-3">
                <Avatar>
                  <AvatarFallback aria-label="Example person">
                    EX
                  </AvatarFallback>
                </Avatar>
                <Avatar>
                  <AvatarFallback aria-label="Unnamed person">?</AvatarFallback>
                </Avatar>
              </div>
            </State>
          </div>
        </Section>
        <Section title="Product vocabulary">
          <div className="grid min-w-0 gap-5 sm:grid-cols-2">
            <State title="Status pills">
              <StatusPill status="ready" reason="Accepting requests." />
              <StatusPill
                status="starting"
                reason="Getting ready to accept requests."
              />
              <StatusPill
                status="busy"
                reason="Your request will wait in the queue."
              />
              <StatusPill
                status="unavailable"
                reason="The engine is offline."
              />
            </State>
            <State title="Capability icons">
              <div className="flex flex-wrap gap-3">
                {(
                  [
                    "chat",
                    "image",
                    "video",
                    "voice",
                    "transcribe",
                    "decide",
                    "embed",
                  ] as Capability[]
                ).map((capability) => (
                  <CapabilityIcon key={capability} capability={capability} />
                ))}
              </div>
            </State>
            <State title="Copy field">
              <CopyField value="https://gateway.example/v1" />
              <CopyField value="" label="Unavailable endpoint" disabled />
              <CopyField value="" label="Loading endpoint" loading />
              <CopyField
                value=""
                label="Endpoint error"
                error="The endpoint could not be loaded."
              />
            </State>
            <State title="Secret reveal · example only">
              <SecretReveal secret="example-key-not-a-real-secret" />
              <SecretReveal secret={null} />
            </State>
            <State title="Relative time">
              <RelativeTime ts="2026-10-01T12:00:00Z" />
              <RelativeTime ts={null} fallback="Not used yet" />
            </State>
            <State title="Money">
              <Money local amount={0} />
              <Money amount={0} />
              <Money amount={0.00004} />
              <Money amount={null} />
            </State>
            <State title="Entity name">
              <EntityName name="My coding assistant" detail="Personal app" />
              <EntityName name={null} id="example-app-id" />
            </State>
            <State title="Code block">
              <CodeBlock
                snippets={[
                  {
                    language: "curl",
                    code: "curl https://gateway.example/v1/models",
                  },
                  {
                    language: "Python",
                    code: 'from openai import OpenAI\nclient = OpenAI(base_url="https://gateway.example/v1")',
                  },
                  { language: "Empty", code: "" },
                ]}
              />
            </State>
          </div>
        </Section>
        <Section title="Empty, loading and error">
          <div className="grid min-w-0 gap-6 lg:grid-cols-2">
            <EmptyState
              title="Your library starts here"
              description="Make an image, video or audio clip. Your creations will appear here."
              action={
                <Button
                  variant="outline"
                  onClick={() => toast.info("Example creation action")}
                >
                  Try a model
                </Button>
              }
            />
            <div className="flex flex-col gap-4">
              <ErrorState
                message="The request list could not be loaded. Check your connection and try again."
                onRetry={() => {
                  setRetrying(true);
                  setTimeout(() => setRetrying(false), 1500);
                }}
                retrying={retrying}
              />
              <div
                aria-label="Loading list"
                aria-busy
                className="flex flex-col gap-3"
              >
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
              </div>
            </div>
          </div>
        </Section>
        <Section title="Tables">
          <Table>
            <TableCaption>
              Example requests. No live usage is shown.
            </TableCaption>
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow>
                <TableCell>
                  <EntityName name="Chat" detail="Example model" />
                </TableCell>
                <TableCell>
                  <StatusPill status="ready" />
                </TableCell>
                <TableCell>
                  <Money amount={0} />
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell>Image</TableCell>
                <TableCell>
                  <StatusPill status="unavailable" />
                </TableCell>
                <TableCell>
                  <Money amount={null} />
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Model</TableHead>
                <TableHead>Cost</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow>
                <TableCell colSpan={2}>
                  <EmptyState
                    title="No requests yet"
                    description="Your first request will appear here."
                  />
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
          <ErrorState message="Example table could not be loaded." />
          <Table>
            <TableBody>
              <TableRow>
                <TableCell>
                  <Skeleton className="h-4 w-24" />
                </TableCell>
                <TableCell>
                  <Skeleton className="h-4 w-16" />
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </Section>
      </div>
    </TooltipProvider>
  );
}
