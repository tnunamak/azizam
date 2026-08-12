/** The gateway component library (docs/design/WEB_UI_STEERING.md).
 * Token-only styling; every interactive component ships hover /
 * focus-visible / disabled / loading states. App code imports from here;
 * nothing in this directory imports from app/. */

export { Badge, type BadgeProps, type BadgeTone } from "./badge";
export {
  Button,
  type ButtonProps,
  type ButtonSize,
  type ButtonVariant,
} from "./button";
export {
  MetricTrendCard,
  TrendBarChart,
  TrendLineChart,
  type ChartPoint,
  type ChartTone,
  type ChartAccessibility,
  hasMinimumChartSamples,
  type MetricTrendCardProps,
  StatusChart,
  type StatusChartLegendItem,
  type StatusChartProps,
  StatusUptime,
  type StatusUptimeProps,
  type TrendBarChartProps,
  type TrendLineChartProps,
} from "./charts";
export { Checkbox, type CheckboxProps } from "./checkbox";
export { CompactTouchTargets } from "./compact-touch-targets";
export { ConfirmDialog, type ConfirmDialogProps } from "./confirm-dialog";
export {
  CopyButton,
  type CopyButtonProps,
  copyText,
  useCopied,
} from "./copy-button";
export {
  DataTable,
  type DataTableColumn,
  type DataTableProps,
} from "./data-table";
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
} from "./dialog";
export { EmptyState, type EmptyStateProps } from "./empty-state";
export { EntityRef, type EntityRefProps } from "./entity-ref";
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
export { Input, type InputProps } from "./input";
export {
  OverflowMenu,
  OverflowMenuItem,
  type OverflowMenuItemProps,
  type OverflowMenuProps,
  OverflowMenuSeparator,
} from "./overflow-menu";
export {
  RadioCard,
  type RadioCardProps,
  RadioCards,
  type RadioCardsProps,
} from "./radio-cards";
export { RelativeTime, type RelativeTimeProps } from "./relative-time";
export { SecretMask, type SecretMaskProps } from "./secret-mask";
export { SectionHeader, type SectionHeaderProps } from "./section-header";
export {
  AccountChip,
  ActionCard,
  AmbientBackdrop,
  BrandLockup,
  CodeBlock,
  MobileTabBar,
  MobileTabAction,
  NoticeBanner,
  ObjectList,
  ObjectRow,
  PageLayout,
  PanelHeader,
  ProductPanel,
  SectionKicker,
  StatusCluster,
  type ActionCardProps,
  type BrandLockupProps,
  type CodeBlockProps,
  type MobileNavItem,
  type MobileTabActionProps,
  type NoticeBannerProps,
  type ObjectRowProps,
  type PageLayoutProps,
  type ProductPanelProps,
  type StatusClusterProps,
} from "./surface";
export {
  SummaryStrip,
  type SummaryStripItem,
  type SummaryStripProps,
} from "./summary-strip";
export {
  Sheet,
  SheetClose,
  SheetContent,
  type SheetContentProps,
  SheetDescription,
  SheetFooter,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "./sheet";
export { Skeleton } from "./skeleton";
export { Select, type SelectProps } from "./select";
export { Spinner } from "./spinner";
export { Switch, type SwitchProps } from "./switch";
export { Toaster, toast } from "./toast";
export {
  ActivityFeedRow,
  type ActivityFeedRowProps,
  AppAccessRow,
  type AppAccessRowProps,
  AppAccessRowHeader,
  type AppAccessRowHeaderProps,
  Avatar,
  avatarToneFor,
  type AvatarProps,
  type AvatarTone,
  CommandSearch,
  type CommandSearchItem,
  type CommandSearchProps,
  CopyField,
  type CopyFieldProps,
  EmptyOperationalState,
  type EmptyOperationalStateProps,
  FilterToolbar,
  type FilterToolbarProps,
  type HealthTone,
  IncidentBanner,
  type IncidentBannerProps,
  KeyRevealBlock,
  type KeyRevealBlockProps,
  MetricStrip,
  type MetricStripItem,
  type MetricStripProps,
  MiniSparkline,
  type MiniSparklinePoint,
  MobileActionDock,
  type MobileActionDockProps,
  MobileTopRail,
  type MobileTopRailProps,
  QuickStartChecklist,
  type QuickStartChecklistItem,
  type QuickStartChecklistProps,
  RightContextPanel,
  type RightContextPanelProps,
  type RightContextPanelSection,
  SectionActionHeader,
  type SectionActionHeaderProps,
  ServiceHealthRow,
  type ServiceHealthRowProps,
  StatusAdvisory,
  type StatusAdvisoryItem,
  type StatusAdvisoryProps,
  CurrentAdvisory,
  type CurrentAdvisoryItem,
  type CurrentAdvisoryProps,
  StatusDotLabel,
  type StatusDotLabelProps,
  SystemHealthBand,
  type SystemHealthBandProps,
  TelemetryCard,
  type TelemetryCardProps,
} from "./operational";
