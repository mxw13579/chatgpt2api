import type { ImageReference } from "@/lib/api";
import { resolveImageReferences } from "@/lib/image-storage";
import { isR2Image } from "@/lib/image-reference";
import type { ImageConversation } from "@/store/image-conversations";

export type ResolvedReference = ImageReference & { url: string };

export function applyResolvedReferences(conversations: ImageConversation[], resolved: ResolvedReference[], requestedIds?: Set<string | undefined>): ImageConversation[] {
  const byId = new Map(resolved.map((image) => [image.image_id, image]));
  const resolve = (image: ImageReference): ResolvedReference => {
    const current = byId.get(image.image_id);
    return current
      ? { ...image, ...current, expired: Boolean(current.expired), unavailable: Boolean(current.unavailable) }
      : { ...image, url: "", unavailable: true };
  };
  return conversations.map((conversation) => ({
    ...conversation,
    // URL 刷新不改变对话排序或任务状态。
    turns: conversation.turns.map((turn) => ({
      ...turn,
      images: turn.images.map((image) => isR2Image(image) && (!requestedIds || requestedIds.has(image.image_id)) ? { ...image, ...resolve(image), b64_json: undefined } : image),
      referenceImages: turn.referenceImages.map((image) => {
        if (!isR2Image(image) || (requestedIds && !requestedIds.has(image.image_id))) return image;
        const current = resolve(image);
        return { ...image, ...current, dataUrl: current.url };
      }),
    })),
  }));
}

export async function refreshConversationReferences(conversations: ImageConversation[]) {
  const references = conversations.flatMap((conversation) => conversation.turns.flatMap((turn) =>
    [...(turn.resultsDeleted ? [] : turn.images), ...turn.referenceImages].filter(isR2Image),
  ));
  const unique = [...new Map(references.map((image) => [image.image_id, image])).values()];
  const items: ResolvedReference[] = [];
  for (let offset = 0; offset < unique.length; offset += 100) {
    items.push(...(await resolveImageReferences(unique.slice(offset, offset + 100))).items);
  }
  return applyResolvedReferences(conversations, items);
}
