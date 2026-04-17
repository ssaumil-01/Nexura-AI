import React from 'react';
import { useAppStore } from '../../store/useAppStore';

const CodeEditor: React.FC = () => {
  const activeFile = useAppStore((state) => state.activeFile);
  const fileContent = useAppStore((state) => state.fileContent);
  const setFileContent = useAppStore((state) => state.setFileContent);

  if (!activeFile) {
    return (
      <div className="editor-empty">
        <div className="empty-message">No file selected</div>
      </div>
    );
  }

  return (
    <div className="editor-container">
      <div className="editor-header">
        <span className="file-tab active">{activeFile.split('/').pop()}</span>
      </div>
      <div className="editor-main">
        <textarea 
          className="editor-textarea"
          placeholder="// Start coding..."
          value={fileContent}
          onChange={(e) => setFileContent(e.target.value)}
        />
      </div>
    </div>
  );
};

export default CodeEditor;
