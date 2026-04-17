import { create } from 'zustand';

export interface FileEntry {
  path: string;
  name: string;
  is_dir: bool;
}

interface AppStore {
  selectedFolder: string | null;
  files: FileEntry[];
  activeFile: string | null;
  fileContent: string;
  setSelectedFolder: (folder: string | null) => void;
  setFiles: (files: FileEntry[]) => void;
  setActiveFile: (file: string | null) => void;
  setFileContent: (content: string) => void;
}

export const useAppStore = create<AppStore>((set) => ({
  selectedFolder: null,
  files: [],
  activeFile: null,
  fileContent: '',
  
  setSelectedFolder: (folder) => set({ 
    selectedFolder: folder, 
    files: [], 
    activeFile: null, 
    fileContent: '' 
  }),
  
  setFiles: (files) => set({ files }),
  
  setActiveFile: (file) => set({ 
    activeFile: file,
  }),
  
  setFileContent: (content) => set({ fileContent: content }),
}));
