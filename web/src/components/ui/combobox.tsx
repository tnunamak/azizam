"use client";

// Radix combobox composition from shadcn's documented Popover + Command pattern.
import { useId, useState } from "react";
import { Check, ChevronsUpDown, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

export interface ComboboxOption {
  value: string;
  label: string;
  disabled?: boolean;
}
export interface ComboboxProps {
  options: ComboboxOption[];
  value?: string;
  onValueChange: (value: string) => void;
  label: string;
  placeholder?: string;
  disabled?: boolean;
  loading?: boolean;
  error?: string;
}
export function Combobox({
  options,
  value,
  onValueChange,
  label,
  placeholder = "Select an option",
  disabled,
  loading,
  error,
}: ComboboxProps) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            type="button"
            variant="outline"
            role="combobox"
            aria-label={label}
            aria-expanded={open}
            aria-controls={id}
            aria-invalid={!!error}
            disabled={disabled || loading}
            className="w-full justify-between"
          >
            <span className="truncate">
              {loading
                ? "Loading options…"
                : options.find((o) => o.value === value)?.label || placeholder}
            </span>
            {loading ? (
              <Loader2 className="animate-spin" />
            ) : (
              <ChevronsUpDown />
            )}
          </Button>
        </PopoverTrigger>
        <PopoverContent
          className="w-[var(--radix-popover-trigger-width)] p-0"
          align="start"
        >
          <Command>
            <CommandInput
              placeholder={`Search ${label.toLowerCase()}…`}
              aria-label={`Search ${label.toLowerCase()}`}
            />
            <CommandList id={id}>
              <CommandEmpty>No options found.</CommandEmpty>
              <CommandGroup>
                {options.map((o) => (
                  <CommandItem
                    key={o.value}
                    value={o.label}
                    disabled={o.disabled}
                    onSelect={() => {
                      onValueChange(o.value);
                      setOpen(false);
                    }}
                  >
                    {o.label}
                    {value === o.value && <Check className="ml-auto" />}
                  </CommandItem>
                ))}
              </CommandGroup>
            </CommandList>
          </Command>
        </PopoverContent>
      </Popover>
      {error && <p className="text-destructive text-sm">{error}</p>}
    </div>
  );
}
