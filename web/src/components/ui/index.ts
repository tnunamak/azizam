// Legacy API retained until the remaining admin surfaces migrate.
/** The gateway component library (docs/design/WEB_UI_STEERING.md).
 * Token-only styling; every interactive component ships hover /
 * focus-visible / disabled / loading states. App code imports from here;
 * nothing in this directory imports from app/. */

export {
  Button,
  type ButtonProps,
  type ButtonSize,
  type ButtonVariant,
} from "./legacy/button";
export { CompactTouchTargets } from "./compact-touch-targets";
export { ConfirmDialog, type ConfirmDialogProps } from "./confirm-dialog";
export {
  CopyButton,
  type CopyButtonProps,
  copyText,
  useCopied,
} from "./copy-button";
export {
  Dialog,
  DialogBody,
  DialogClose,
  DialogContent,
  type DialogContentProps,
  DialogDescription,
  DialogFooter,
  DialogForm,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "./legacy/dialog";
export { Field, type FieldProps, useFieldContext } from "./field";
export {
  IconButton,
  type IconButtonProps,
  type IconButtonSize,
  type IconButtonVariant,
} from "./icon-button";
export {
  AuditIcon,
  AppsIcon,
  CheckIcon,
  ChevronDownIcon,
  CopyIcon,
  DotsIcon,
  ExternalLinkIcon,
  MenuIcon,
  OverviewIcon,
  ProfilesIcon,
  ServicesIcon,
  SettingsIcon,
  StatusIcon,
  type StatusIconTone,
  UsersIcon,
  XIcon,
} from "./icons";
export { Input, type InputProps } from "./legacy/input";
export {
  OverflowMenu,
  OverflowMenuItem,
  type OverflowMenuItemProps,
  type OverflowMenuProps,
  OverflowMenuSeparator,
} from "./overflow-menu";
export { RelativeTime, type RelativeTimeProps } from "./relative-time";
export { Skeleton } from "./legacy/skeleton";
export { Spinner } from "./spinner";
export { Toaster, toast } from "./toast";
export {
  EmptyOperationalState,
  type EmptyOperationalStateProps,
  IncidentBanner,
  type IncidentBannerProps,
} from "./operational";
