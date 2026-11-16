import React from 'react';
import { useAppStore } from '../../store/useAppStore';
import { VscCode, VscClose } from 'react-icons/vsc';
import Editor from '@monaco-editor/react';
import { getIconForFile } from 'vscode-icons-js';

const ICON_BASE_URL = 'https://raw.githubusercontent.com/vscode-icons/vscode-icons/master/icons';

const EditorArea: React.FC = () => {
  const activeFile = useAppStore((state) => state.activeFile);
  const openFiles = useAppStore((state) => state.openFiles);
  const fileContents = useAppStore((state) => state.fileContents);
  const setFileContent = useAppStore((state) => state.setFileContent);
  const setActiveFile = useAppStore((state) => state.setActiveFile);
  const closeFile = useAppStore((state) => state.closeFile);

  if (!activeFile || openFiles.length === 0) {
    return (
      <main className="editor-area editor-empty">
        <div className="empty-message">
          <VscCode size={64} style={{ opacity: 0.2 }} />
          <span>No file selected</span>
        </div>
      </main>
    );
  }

  const activeContent = fileContents[activeFile] || '';
  const activeFileName = activeFile.split(/[\\/]/).pop() || '';

  // Helper to get custom language for specific files that Monaco doesn't highlight by default
  const getLanguage = (fileName: string) => {
    const name = fileName.toLowerCase();
    if (name === '.env' || name.endsWith('.env')) return 'ini';
    if (name === 'requirements.txt') return 'ini';
    if (name === '.gitignore') return 'shell';
    return undefined; // Let Monaco infer automatically from the path
  };

  return (
    <main className="editor-area">
      <div className="editor-container">
        <div className="editor-header tabs-container">
          {openFiles.map((file) => {
            const fileName = file.split(/[\\/]/).pop() || '';
            const isActive = file === activeFile;
            const fileIcon = getIconForFile(fileName);
            
            return (
              <div 
                key={file}
                className={`file-tab ${isActive ? 'active' : ''}`}
                onClick={() => setActiveFile(file)}
              >
                <img 
                  src={`${ICON_BASE_URL}/${fileIcon}`} 
                  alt="" 
                  style={{ width: '14px', height: '14px', marginRight: '6px', objectFit: 'contain' }}
                  onError={(e) => {
                    (e.target as HTMLImageElement).style.display = 'none';
                  }}
                />
                <span className="tab-title">{fileName}</span>
                <div 
                  className="tab-close"
                  onClick={(e) => {
                    e.stopPropagation();
                    closeFile(file);
                  }}
                >
                  <VscClose size={14} />
                </div>
              </div>
            );
          })}
        </div>
        <div className="editor-main" style={{ flex: 1, position: 'relative' }}>
          <Editor
            height="100%"
            path={activeFile}
            language={getLanguage(activeFileName)}
            theme="vs-dark"
            value={activeContent}
            onChange={(value) => setFileContent(activeFile, value || '')}
            options={{
              minimap: { enabled: false },
              fontSize: 14,
              fontFamily: "var(--font-mono)",
              wordWrap: 'on',
              padding: { top: 16 },
              scrollBeyondLastLine: false,
              smoothScrolling: true,
              cursorBlinking: "smooth",
              fontLigatures: false,
            }}
          />
        </div>
      </div>
    </main>
  );
};

export default EditorArea;