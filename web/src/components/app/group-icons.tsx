import {
  Banknote,
  BookOpenCheck,
  Boxes,
  Clock3,
  FileUp,
  Globe2,
  Link2,
  Megaphone,
  Plug,
  Star,
  Store,
  Type,
  type LucideIcon,
} from "lucide-react";

export const GROUP_ICONS: Record<string, LucideIcon> = {
  financials: Banknote,
  inventory: Boxes,
  presence: Globe2,
  reputation: Star,
  longevity: Clock3,
  community: Megaphone,
  custom: FileUp,
};

export const ITEM_ICONS: Record<string, LucideIcon> = {
  bank: Banknote,
  accounting: BookOpenCheck,
  sales: Store,
};

export const KIND_ICONS: Record<string, LucideIcon> = {
  connector: Plug,
  upload: FileUp,
  link: Link2,
  field: Type,
};

export const KIND_LABELS: Record<string, string> = {
  connector: "Connect",
  upload: "Upload",
  link: "Link",
  field: "Answer",
};
