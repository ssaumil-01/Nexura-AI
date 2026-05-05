import React, { useState, useCallback, useRef } from 'react';
import Sidebar from './Sidebar';
import EditorArea from './EditorArea';
import ChatPanel from '../chat/ChatPanel';
import { VscRobot } from 'react-icons/vsc';
import './Layout.css';

const MainLayout: React.FC = () => {
  const [sidebarWidth, setSidebarWidth] = useState(250);
  const [chatWidth, setChatWidth] = useState(360);
  const [isResizingSidebar, setIsResizingSidebar] = useState(false);
  const [isResizingChat, setIsResizingChat] = useState(false);

  const startResizingSidebar = useCallback(() => setIsResizingSidebar(true), []);
  const startResizingChat = useCallback(() => setIsResizingChat(true), []);

  const stopResizing = useCallback(() => {
    setIsResizingSidebar(false);
    setIsResizingChat(false);
  }, []);

  const resize = useCallback(
    (mouseMoveEvent: MouseEvent) => {
      if (isResizingSidebar) {
        const newWidth = mouseMoveEvent.clientX;
        if (newWidth > 150 && newWidth < 500) {
          setSidebarWidth(newWidth);
        }
      } else if (isResizingChat) {
        const newWidth = window.innerWidth - mouseMoveEvent.clientX;
        if (newWidth > 200 && newWidth < 600) {
          setChatWidth(newWidth);
        }
      }
    },
    [isResizingSidebar, isResizingChat]
  );

  React.useEffect(() => {
    window.addEventListener('mousemove', resize);
    window.addEventListener('mouseup', stopResizing);
    return () => {
      window.removeEventListener('mousemove', resize);
      window.removeEventListener('mouseup', stopResizing);
    };
  }, [resize, stopResizing]);

  return (
    <div className="main-layout-horizontal" style={{ cursor: isResizingSidebar || isResizingChat ? 'col-resize' : 'auto' }}>
      <div style={{ width: sidebarWidth, flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
        <Sidebar />
      </div>
      
      <div className="resize-handle" onMouseDown={startResizingSidebar} data-resize-handle-state={isResizingSidebar ? 'drag' : ''} />
      
      <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
        <EditorArea />
      </div>
      
      <div className="resize-handle" onMouseDown={startResizingChat} data-resize-handle-state={isResizingChat ? 'drag' : ''} />
      
      <div style={{ width: chatWidth, flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
        <aside className="right-panel" style={{ width: '100%' }}>
          <div className="section-header">
            <VscRobot size={16} /> Chat Assistant
          </div>
          <ChatPanel />
        </aside>
      </div>
    </div>
  );
};

export default MainLayout;