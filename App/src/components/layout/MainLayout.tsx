import React from 'react';
import Sidebar from './Sidebar';
import EditorArea from './EditorArea';
import ChatPanel from '../chat/ChatPanel';
import './Layout.css';

const MainLayout: React.FC = () => {
  return (
    <div className="main-layout-horizontal">
      <Sidebar />
      <EditorArea />
      <aside className="right-panel">
        <div className="section-header">Chat Assistant</div>
        <ChatPanel />
      </aside>
    </div>
  );
};

export default MainLayout;