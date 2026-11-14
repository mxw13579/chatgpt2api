import type { ImageReference } from "@/lib/api";
import { resolveImageReferences } from "@/lib/image-storage";
import { isR2Image, mergeResolvedImageReference } from "@/lib/image-reference";
import type { ImageConversation } from "@/store/image-conversations";

export type ResolvedReference = ImageReference & { url: string };

export function applyResolvedReferences(conversations: ImageConversation[], resolved: ResolvedReference[], requestedIds?: Set<string | undefined>): ImageConversation[] {
  const byId = new Map(resolved.map((image) => [image.image_id, image]));
  const resolve = (image: ImageReference) => mergeResolvedImageReference(image, byId.get(image.image_id));
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

export function getConversationReferences(conversations: ImageConversation[], activeReferences: ImageReference[] = []) {
  const references = [
    ...conversations.flatMap((conversation) => conversation.turns.flatMap((turn) =>
      [...(turn.resultsDeleted ? [] : turn.images), ...turn.referenceImages],
    )),
    ...activeReferences,
  ].filter(isR2Image);
  return [...new Map(references.map((image) => [image.image_id, image])).values()];
}

export async function resolveHistoryReferences(references: ImageReference[]) {
  const items: ResolvedReference[] = [];
  for (let offset = 0; offset < references.length; offset += 100) {
    items.push(...(await resolveImageReferences(references.slice(offset, offset + 100))).items);
  }
  return items;
}

export async function refreshConversationReferences(conversations: ImageConversation[]) {
  const references = getConversationReferences(conversations);
  const items = await resolveHistoryReferences(references);
  return applyResolvedReferences(conversations, items, new Set(references.map((image) => image.image_id)));
}
