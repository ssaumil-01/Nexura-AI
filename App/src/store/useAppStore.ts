import { create } from 'zustand';

export interface FileEntry {
  path: string;
  name: string;
  is_dir: boolean;
}

interface AppStore {
  selectedFolder: string | null;
  files: FileEntry[];
  
  openFiles: string[];
  activeFile: string | null;
  fileContents: Record<string, string>;
  
  setSelectedFolder: (folder: string | null) => void;
  setFiles: (files: FileEntry[]) => void;
  
  addOpenFile: (file: string) => void;
  closeFile: (file: string) => void;
  setActiveFile: (file: string | null) => void;
  setFileContent: (file: string, content: string) => void;
}

export const useAppStore = create<AppStore>((set, get) => ({
  selectedFolder: null,
  files: [],
  
  openFiles: [],
  activeFile: null,
  fileContents: {},
  
  setSelectedFolder: (folder) => set({ 
    selectedFolder: folder, 
    files: [], 
    openFiles: [],
    activeFile: null, 
    fileContents: {} 
  }),
  
  setFiles: (files) => set({ files }),
  
  addOpenFile: (file) => {
    const { openFiles } = get();
    if (!openFiles.includes(file)) {
      set({ openFiles: [...openFiles, file] });
    }
  },
  
  closeFile: (file) => {
    const { openFiles, activeFile, fileContents } = get();
    const newOpenFiles = openFiles.filter(f => f !== file);
    
    // Cleanup cache
    const newFileContents = { ...fileContents };
    delete newFileContents[file];
    
    // Determine new active file if we are closing the current one
    let newActiveFile = activeFile;
    if (activeFile === file) {
      newActiveFile = newOpenFiles.length > 0 ? newOpenFiles[newOpenFiles.length - 1] : null;
    }
    
    set({
      openFiles: newOpenFiles,
      activeFile: newActiveFile,
      fileContents: newFileContents
    });
  },
  
  setActiveFile: (file) => set({ activeFile: file }),
  
  setFileContent: (file, content) => {
    set((state) => ({
      fileContents: {
        ...state.fileContents,
        [file]: content
      }
    }));
  },
}));
