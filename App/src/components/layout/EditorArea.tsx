import React from 'react';
import { useAppStore } from '../../store/useAppStore';

const EditorArea: React.FC = () => {
  const activeFile = useAppStore((state) => state.activeFile);
  const fileContent = useAppStore((state) => state.fileContent);
  const setFileContent = useAppStore((state) => state.setFileContent);

  if (!activeFile) {
    return (
      <main className="editor-area editor-empty">
        <div className="empty-message">No file selected</div>
      </main>
    );
  }

  return (
    <main className="editor-area">
      <div className="editor-container">
        <div className="editor-header">
          <div className="file-tab active">
            {activeFile.split('/').pop()}
          </div>
        </div>
        <div className="editor-main">
          <textarea
            className="editor-textarea"
            value={fileContent}
            onChange={(e) => setFileContent(e.target.value)}
            spellCheck={false}
          />
        </div>
      </div>
    </main>
  );
};

export default EditorArea;