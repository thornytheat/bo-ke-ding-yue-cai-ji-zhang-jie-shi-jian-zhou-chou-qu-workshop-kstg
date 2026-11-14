import { authStore } from '../stores/authStore';
import { http } from './client';
import { ApiError } from './client';
import type { ExportPackage } from '../types/models';

/** 包内固定文件名，与后端 export_service 常量一致。 */
export const EXPORT_FILES = {
  clipList: 'clip-list.csv',
  chapterCuts: 'chapter-cuts.csv',
  issues: 'issues.csv',
  manifest: 'manifest.json',
} as const;

async function authHeaders(): Promise<Record<string, string>> {
  const token = authStore.getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function safeParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

/** 触发浏览器下载并返回字节数（复用于历史/当前包的 zip 与单文件）。 */
async function saveBlob(response: Response, fallbackName: string): Promise<number> {
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  const disposition = response.headers.get('Content-Disposition') ?? '';
  const matched = /filename="([^"]+)"/.exec(disposition);
  anchor.download = matched?.[1] ?? fallbackName;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  return blob.size;
}

export const exportApi = {
  list: (limit = 50) => http.get<{ items: ExportPackage[] }>(`/exports/packages?limit=${limit}`),

  publish: () => http.post<{ package: ExportPackage; reused: boolean }>('/exports/packages'),

  detail: (code: string) => http.get<{ package: ExportPackage }>(`/exports/packages/${code}`),

  downloadZip: async (code: string): Promise<number> => {
    const response = await fetch(`/api/exports/packages/${code}/zip`, {
      headers: await authHeaders(),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new ApiError(response.status, text ? safeParse(text) : null);
    }
    return saveBlob(response, `${code}.zip`);
  },

  downloadFile: async (code: string, filename: string): Promise<number> => {
    const response = await fetch(`/api/exports/packages/${code}/files/${filename}`, {
      headers: await authHeaders(),
    });
    if (!response.ok) {
      const text = await response.text();
      throw new ApiError(response.status, text ? safeParse(text) : null);
    }
    return saveBlob(response, `${code}-${filename}`);
  },
};
