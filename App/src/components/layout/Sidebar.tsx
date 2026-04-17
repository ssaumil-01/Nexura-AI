import React, { useEffect } from 'react';
import { open } from '@tauri-apps/plugin-dialog';
import { invoke } from '@tauri-apps/api/core';
import { useAppStore } from '../../store/useAppStore';
import FileTree from '../file-tree/FileTree';

const Sidebar: React.FC = () => {
  const setSelectedFolder = useAppStore((state) => state.setSelectedFolder);
  const selectedFolder = useAppStore((state) => state.selectedFolder);
  const files = useAppStore((state) => state.files);
  const setFiles = useAppStore((state) => state.setFiles);

  // Fetch files whenever selectedFolder changes
  useEffect(() => {
    const fetchFiles = async () => {
      if (!selectedFolder) return;
      
      try {
        console.log('Fetching files for:', selectedFolder);
        const result = await invoke('list_dir', { path: selectedFolder });
        setFiles(result as any);
      } catch (err) {
        console.error('Failed to list directory:', err);
        // You might want to show an error message to the user here
      }
    };

    fetchFiles();
  }, [selectedFolder, setFiles]);

  const handleOpenFolder = async () => {
    try {
      const selected = await open({
        directory: true,
        multiple: false,
        title: 'Select Project Folder'
      });
      
      if (selected) {
        setSelectedFolder(selected as string);
      }
    } catch (err) {
      console.error('Failed to open directory picker:', err);
    }
  };

  return (
    <aside className="sidebar">
      <div className="section-header">Explorer</div>
      
      <div className="sidebar-actions">
        <button className="action-button" onClick={handleOpenFolder}>
          {selectedFolder ? 'Change Folder' : 'Open Folder'}
        </button>
      </div>

      <div className="file-tree-container">
        {selectedFolder ? (
          <FileTree files={files} />
        ) : (
          <div className="tree-placeholder">No folder selected</div>
        )}
      </div>
    </aside>
  );
};

export default Sidebar;