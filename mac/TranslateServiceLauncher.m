#import <Foundation/Foundation.h>
#include <stdio.h>
#include <unistd.h>

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSString *executablePath = [NSString stringWithUTF8String:argv[0]];
        if (![executablePath isAbsolutePath]) {
            executablePath = [[NSFileManager defaultManager].currentDirectoryPath
                stringByAppendingPathComponent:executablePath];
        }
        executablePath = [executablePath stringByStandardizingPath];
        NSString *appPath = [[[executablePath stringByDeletingLastPathComponent]
            stringByDeletingLastPathComponent]
            stringByDeletingLastPathComponent];
        NSString *platformPath = [appPath stringByDeletingLastPathComponent];
        NSString *workspacePath = [platformPath stringByDeletingLastPathComponent];
        NSString *workerPath = [workspacePath stringByAppendingPathComponent:@"source/worker"];
        NSString *launcherPath = [workerPath stringByAppendingPathComponent:@"run_standalone.sh"];

        NSString *dataPath = [workerPath stringByAppendingPathComponent:@"data"];
        [[NSFileManager defaultManager] createDirectoryAtPath:dataPath
                                   withIntermediateDirectories:YES
                                                    attributes:nil
                                                         error:nil];
        NSString *logPath = [dataPath stringByAppendingPathComponent:@"app-launch.log"];
        FILE *logFile = fopen(logPath.fileSystemRepresentation, "a");
        if (logFile != NULL) {
            dup2(fileno(logFile), STDOUT_FILENO);
            dup2(fileno(logFile), STDERR_FILENO);
            fclose(logFile);
        }

        setenv("TRANSLATE_DATA_DIR", dataPath.fileSystemRepresentation, 1);
        const char *currentPath = getenv("PATH");
        NSString *path = [NSString stringWithFormat:@"/opt/homebrew/opt/ffmpeg-full/bin:/opt/homebrew/bin:/usr/local/opt/ffmpeg-full/bin:/usr/local/bin:/usr/bin:/bin:%s", currentPath ?: ""];
        setenv("PATH", path.UTF8String, 1);
        chdir(workerPath.fileSystemRepresentation);
        execl("/bin/bash", "bash", launcherPath.fileSystemRepresentation, (char *)NULL);
        perror("Translate Service could not start");
        return 1;
    }
    return 0;
}
